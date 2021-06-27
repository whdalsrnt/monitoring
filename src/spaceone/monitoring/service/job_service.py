import logging
from typing import List, Union
from datetime import timedelta, datetime

from spaceone.core.service import *
from spaceone.core.error import *
from spaceone.core import utils, cache, config
from spaceone.monitoring.model.alert_model import Alert
from spaceone.monitoring.model.project_alert_config_model import ProjectAlertConfig
from spaceone.monitoring.model.escalation_policy_model import EscalationPolicy
from spaceone.monitoring.manager.alert_manager import AlertManager
from spaceone.monitoring.manager.identity_manager import IdentityManager
from spaceone.monitoring.manager.webhook_manager import WebhookManager
from spaceone.monitoring.manager.project_alert_config_manager import ProjectAlertConfigManager
from spaceone.monitoring.manager.escalation_policy_manager import EscalationPolicyManager
from spaceone.monitoring.manager.notification_manager import NotificationManager
from spaceone.monitoring.manager.job_manager import JobManager

_LOGGER = logging.getLogger(__name__)


@authentication_handler
@authorization_handler
@mutation_handler
@event_handler
class JobService(BaseService):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.job_mgr: JobManager = self.locator.get_manager('JobManager')

    @transaction(append_meta={'authorization.scope': 'SYSTEM'})
    @check_required(['domain_id'])
    def create_job(self, params):
        """ Create job

        Args:
            params (dict): {
                'domain_id': 'str'
            }

        Returns:
            None
        """

        domain_id = params['domain_id']

        if self.job_mgr.is_domain_job_running(domain_id):
            _LOGGER.debug(f'[create_job] Job is running. (domain_id = {domain_id})')
            return None

        job_vo = self.job_mgr.create_job(domain_id)
        try:
            alert_vos, total_count = self._list_open_alerts(domain_id)

            if total_count > 0:
                job_vo.update({'total_tasks': total_count, 'remained_tasks': total_count})

                for alert_vo in alert_vos:
                    _LOGGER.debug(f'[create_job] Push task (JobService.create_notification): {alert_vo.alert_id}')
                    self.job_mgr.push_task('monitoring_alert_notification_from_scheduler',
                                           'JobService',
                                           'create_notification',
                                           {
                                               'job_id': job_vo.job_id,
                                               'alert_id': alert_vo.alert_id,
                                               'domain_id': alert_vo.domain_id
                                           })
            else:
                job_vo.delete()
        except Exception as e:
            self.job_mgr.change_error_status(job_vo, e)
            self.transaction.execute_rollback()

    @transaction(append_meta={'authorization.scope': 'SYSTEM'})
    @check_required(['alert_id', 'domain_id'])
    def create_notification(self, params):
        """ Create job

        Args:
            params (dict): {
                'job_id': 'str',
                'alert_id': 'str',
                'domain_id_id':
            }

        Returns:
            None
        """

        job_id = params.get('job_id')
        alert_id = params['alert_id']
        domain_id = params['domain_id']

        job_mgr: JobManager = self.locator.get_manager('JobManager')

        try:
            alert_mgr: AlertManager = self.locator.get_manager('AlertManager')

            alert_vo: Alert = alert_mgr.get_alert(alert_id, domain_id)
            project_id = alert_vo.project_id
            escalation_policy_id = alert_vo.escalation_policy_id

            alert_options = self._get_project_alert_options(project_id, domain_id)
            rules, finish_condition = self._get_escalation_policy_rules_and_finish_condition(escalation_policy_id,
                                                                                             domain_id)

            # Check Notification Urgency and Finish Condition
            if not (self._check_notification_options(alert_vo, alert_options)
                    and self._check_finish_condition(alert_vo, finish_condition)):
                alert_mgr.update_alert_by_vo({'escalation_ttl': 0}, alert_vo)

            else:
                # Escalate Alert
                is_notify, alert_vo = self._check_escalation_time_and_escalate_alert(alert_mgr, alert_vo, rules)
                if is_notify:
                    notification_mgr: NotificationManager = self.locator.get_manager('NotificationManager')
                    message = self._create_notification_message(alert_vo, rules)
                    notification_mgr.create_notification(message)

            if job_id:
                job_vo = self.job_mgr.get_job(job_id, domain_id)
                job_mgr.decrease_remained_tasks(job_vo)
        except Exception as e:
            if job_id:
                job_vo = self.job_mgr.get_job(job_id, domain_id)
                job_mgr.change_error_status(job_vo, e)

            _LOGGER.error(f'[create_notification] Job Error: {e}', exc_info=True)
            self.transaction.execute_rollback()

    @transaction(append_meta={'authorization.scope': 'SYSTEM'})
    def create_jobs_by_domain(self, params):
        """ Create jobs by domain

        Args:
            params (dict): {}

        Returns:
            None
        """

        for domain_id in self._list_domains_of_alerts():
            _LOGGER.debug(f'[create_jobs_by_domain] Push task (JobService.create): {domain_id}')
            self.job_mgr.push_task('monitoring_alert_job', 'JobService', 'create_job', {'domain_id': domain_id})

    def _list_domains_of_alerts(self):
        query = {
            'distinct': 'domain_id',
            'filter': [
                {
                    'k': 'state',
                    'v': ['TRIGGERED', 'ACKNOWLEDGED'],
                    'o': 'in'
                },
                {
                    'k': 'escalation_ttl',
                    'v': 0,
                    'o': 'gt'
                }
            ]
        }

        alert_mgr: AlertManager = self.locator.get_manager('AlertManager')

        response = alert_mgr.stat_alerts(query)
        return response.get('results', [])

    def _list_open_alerts(self, domain_id) -> List[Alert]:
        alert_mgr: AlertManager = self.locator.get_manager('AlertManager')

        query = {
            'filter': [
                {
                    'k': 'domain_id',
                    'v': domain_id,
                    'o': 'eq'
                },
                {
                    'k': 'state',
                    'v': ['TRIGGERED', 'ACKNOWLEDGED'],
                    'o': 'in'
                },
                {
                    'k': 'escalation_ttl',
                    'v': 0,
                    'o': 'gt'
                }
            ]
        }

        return alert_mgr.list_alerts(query)

    @cache.cacheable(key='project-alert-options:{domain_id}:{project_id}', expire=300)
    def _get_project_alert_options(self, project_id, domain_id):
        project_alert_config_mgr: ProjectAlertConfigManager = self.locator.get_manager('ProjectAlertConfigManager')
        project_alert_config_vo: ProjectAlertConfig = project_alert_config_mgr.get_project_alert_config(project_id,
                                                                                                        domain_id)

        return dict(project_alert_config_vo.options.to_dict())

    @cache.cacheable(key='escalation-policy-condition:{domain_id}:{escalation_policy_id}', expire=300)
    def _get_escalation_policy_rules_and_finish_condition(self, escalation_policy_id, domain_id):
        escalation_policy_mgr: EscalationPolicyManager = self.locator.get_manager('EscalationPolicyManager')
        escalation_policy_vo: EscalationPolicy = escalation_policy_mgr.get_escalation_policy(escalation_policy_id,
                                                                                             domain_id)
        rules = []
        for rule in escalation_policy_vo.rules:
            rules.append(dict(rule.to_dict()))

        return rules, escalation_policy_vo.finish_condition

    @staticmethod
    def _get_current_escalation_rule(alert_vo: Alert, rules):
        return rules[alert_vo.escalation_step - 1]

    @staticmethod
    def _check_notification_options(alert_vo: Alert, alert_options):
        if alert_options['notification_urgency'] == 'HIGH' and alert_vo.urgency == 'LOW':
            _LOGGER.debug(f'[_check_notification_options] End notification. '
                          f'(notification_urgency = HIGH, alert_urgency = LOW, alert_id = {alert_vo.alert_id})')
            return False
        else:
            return True

    @staticmethod
    def _check_finish_condition(alert_vo: Alert, finish_condition):
        if finish_condition == 'ACKNOWLEDGED' and alert_vo.state == 'ACKNOWLEDGED':
            _LOGGER.debug(f'[_check_finish_condition] End notification. '
                          f'(finish_condition = ACKNOWLEDGED, alert_state = ACKNOWLEDGED, '
                          f'alert_id = {alert_vo.alert_id})')
            return True
        else:
            return True

    @staticmethod
    def _check_escalation_time_and_escalate_alert(alert_mgr: AlertManager, alert_vo: Alert, rules):
        current_step = alert_vo.escalation_step
        escalation_ttl = alert_vo.escalation_ttl
        current_rule = rules[current_step - 1]
        escalate_minutes = current_rule.get('escalate_minutes', 0)
        escalated_at: Union[datetime, None] = alert_vo.escalated_at

        # First triggered alert
        if escalated_at is None:
            escalated_alert_vo = alert_mgr.update_alert_by_vo({'escalated_at': datetime.utcnow()}, alert_vo)
            return True, escalated_alert_vo
        else:
            now = datetime.utcnow()

            # now > escalated_at + escalate_minutes
            if now > (escalated_at + timedelta(minutes=escalate_minutes)):
                # When the current step is the maximum
                if len(rules) == current_step:
                    if escalation_ttl == 1:
                        _LOGGER.debug(f'[_check_escalation_time_and_escalate_alert] Max escalation step. '
                                      f'(alert_id = {alert_vo.alert_id})')

                        escalated_alert_vo = alert_mgr.update_alert_by_vo({
                            'escalated_at': datetime.utcnow(),
                            'escalation_ttl': escalation_ttl - 1
                        }, alert_vo)
                    else:
                        _LOGGER.debug(f'[_check_escalation_time_and_escalate_alert] Repeat again from the first step. '
                                      f'(alert_id = {alert_vo.alert_id})')

                        escalated_alert_vo = alert_mgr.update_alert_by_vo({
                            'escalated_at': datetime.utcnow(),
                            'escalation_step': 1,
                            'escalation_ttl': escalation_ttl - 1
                        }, alert_vo)
                else:
                    _LOGGER.debug(f'[_check_escalation_time_and_escalate_alert] Escalate from {current_step} '
                                  f'to {current_step + 1} steps. (alert_id = {alert_vo.alert_id})')

                    escalated_alert_vo = alert_mgr.update_alert_by_vo({
                        'escalated_at': datetime.utcnow(),
                        'escalation_step': current_step + 1
                    }, alert_vo)
                return True, escalated_alert_vo
            else:
                return False, alert_vo

    def _create_notification_message(self, alert_vo: Alert, rules):
        domain_id = alert_vo.domain_id
        current_step = rules[alert_vo.escalation_step - 1]

        tags = {
            'State': alert_vo.state,
            'Project': self._get_project_name(alert_vo.project_id, domain_id),
            'Urgency': alert_vo.urgency,
            'Triggered By': self._get_triggered_by_name(alert_vo.triggered_by, domain_id),
            'Created': utils.datetime_to_iso8601(alert_vo.created_at)
        }

        if alert_vo.status_message != '':
            tags['Status Message'] = alert_vo.status_message

        if alert_vo.assignee:
            tags['Assignee'] = self._get_user_name(alert_vo.assignee, domain_id)

        resource = alert_vo.resource or {}

        if 'name' in resource:
            tags['Resource Name'] = resource['name']

        if 'resource_id' in resource:
            tags['Resource ID'] = resource['resource_id']

        if 'resource_type' in resource:
            tags['Resource Type'] = resource['resource_type']

        title = f'[Alerting] {alert_vo.title}'
        description = alert_vo.description

        # Need to change multiple language
        if 'name' in resource:
            short_message = f'경고! {resource["name"]}에 장애가 발생했습니다.'
        else:
            short_message = f'경고! 장애 발생. {alert_vo.title}'

        # Callback
        access_key = self._generate_access_key()
        callback_url = self._make_callback_url(alert_vo.alert_id, domain_id, access_key)

        return {
            'resource_type': 'identity.Project',
            'resource_id': alert_vo.project_id,
            "notification_type": "ERROR",
            'topic': 'monitoring.Alert',
            'message': {
                'title': title,
                'description': description,
                'tags': tags,
                'short_message': short_message,
                'callbacks': [
                    {
                        'label': 'Acknowledge Alerts',
                        'url': callback_url
                    }
                ]
            },
            'notification_level': current_step['notification_level'],
            'domain_id': alert_vo.domain_id
        }

    @cache.cacheable(key='project-name:{domain_id}:{project_id}', expire=300)
    def _get_project_name(self, project_id, domain_id):
        try:
            identity_mgr: IdentityManager = self.locator.get_manager('IdentityManager')
            project_info = identity_mgr.get_project(project_id, domain_id)
            return f'{project_info["project_group_info"]["name"]} > {project_info["name"]}'
        except Exception as e:
            _LOGGER.error(f'[_get_project_name] Failed to get project: {e}', exc_info=True)

        return project_id

    @cache.cacheable(key='triggered-by-name:{domain_id}:{triggered_by}', expire=300)
    def _get_triggered_by_name(self, triggered_by, domain_id):
        if triggered_by.startswith('webhook-'):
            try:
                webhook_mgr: WebhookManager = self.locator.get_manager('WebhookManager')
                webhook_info = webhook_mgr.get_webhook(triggered_by, domain_id)

                return webhook_info['name']
            except Exception as e:
                _LOGGER.error(f'[_get_triggered_by_name] Failed to get webhook: {e}', exc_info=True)
        else:
            return self._get_user_name(triggered_by, domain_id)

        return triggered_by

    @cache.cacheable(key='user-name:{domain_id}:{user_id}', expire=300)
    def _get_user_name(self, user_id, domain_id):
        try:
            identity_mgr: IdentityManager = self.locator.get_manager('IdentityManager')
            user_info = identity_mgr.get_user(user_id, domain_id)

            if len(user_info.get('name', '').strip()) == 0:
                return user_info['user_id']
            else:
                return f'{user_info["user_id"]} ({user_info["name"]})'
        except Exception as e:
            _LOGGER.error(f'[_get_user_name] Failed to get user: {e}', exc_info=True)

        return user_id

    @staticmethod
    def _generate_access_key():
        return utils.random_string(16)

    def _make_callback_url(self, alert_id, domain_id, access_key):
        def _rollback(alert_id, access_key):
            _LOGGER.info(f'[_make_callback_url._rollback] '
                         f'Delete cache : {alert_id} '
                         f'({access_key})')
            cache.delete(f'alert-notification-callback:{alert_id}:{access_key}')

        cache.set(f'alert-notification-callback:{alert_id}:{access_key}', domain_id, expire=600)
        self.transaction.add_rollback(_rollback, alert_id, access_key)

        webhook_domain = config.get_global('WEBHOOK_DOMAIN')
        return f'{webhook_domain}/monitoring/v1/alert/{alert_id}/{access_key}/ACKNOWLEDGED'