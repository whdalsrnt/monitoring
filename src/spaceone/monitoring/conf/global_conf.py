WEBHOOK_DOMAIN = ''
DATABASE_AUTO_CREATE_INDEX = True
DATABASE_CASE_INSENSITIVE_INDEX = False
DATABASES = {
    'default': {
        'db': 'monitoring',
        'host': 'localhost',
        'port': 27017,
        'username': '',
        'password': ''
    }
}

CACHES = {
    'default': {},
    'local': {
        'backend': 'spaceone.core.cache.local_cache.LocalCache',
        'max_size': 128,
        'ttl': 300
    }
}

HANDLERS = {
}

CONNECTORS = {
    'IdentityConnector': {
    },
    'InventoryConnector': {
    },
    'PluginConnector': {
    },
    'RepositoryConnector': {
    },
    'SecretConnector': {
    },
    'SpaceConnector': {
        'backend': 'spaceone.core.connector.space_connector.SpaceConnector',
        'endpoints': {
            'identity': 'grpc://identity:50051',
            'inventory': 'grpc://inventory:50051',
            'plugin': 'grpc://plugin:50051',
            'repository': 'grpc://repository:50051',
            'secret': 'grpc://secret:50051',
            'notification': 'grpc://notification:50051'

        }
    },
    'DataSourcePluginConnector': {
    },
    'WebhookPluginConnector': {
    },
}

# Scheduler Settings
QUEUES = {}
SCHEDULERS = {}
WORKERS = {}
TOKEN = ""

# Job Settings
JOB_TIMEOUT = 600

# Event Settings
SAME_EVENT_TIME = 600
