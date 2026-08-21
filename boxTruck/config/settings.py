from datetime import timedelta
from pathlib import Path
from decouple import config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config('DEBUG', default=True, cast=bool)

LOCAL = config('LOCAL', default=False, cast=bool)

TEST_ENV = config('TEST_ENV', default=False, cast=bool)

ALLOWED_HOSTS = ["*"]


# Application definition

DJANGO_DEFAULT_APPS = [
    'jazzmin',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

THIRD_APPS = [
    'drf_yasg',
    'rest_framework',
    'rest_framework_simplejwt',
    'django_filters',
    'corsheaders',
    'anymail',
]

MY_APPS = [
    'users',
    'hiring',
    'billing',
    'payroll',
    'analytics',
    'ai',
    'mobile',
    'dispatchers'
]

INSTALLED_APPS = DJANGO_DEFAULT_APPS + THIRD_APPS + MY_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.0/ref/settings/#databases

DB_NAME = None

if TEST_ENV:
    DB_NAME = config('TEST_DB_NAME')
else:
    DB_NAME = config('DB_NAME')

if LOCAL:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': DB_NAME,
            'USER': config('DB_USER'),
            'PASSWORD': config('DB_PASSWORD'),
            'HOST': config('DB_HOST'),
            'PORT': config('DB_PORT'),
        }
    }


# Password validation
# https://docs.djangoproject.com/en/5.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'America/Chicago'

USE_I18N = True

USE_TZ = True

AUTH_USER_MODEL = 'users.CustomUser'

# Default primary key field type
# https://docs.djangoproject.com/en/5.0/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

SWAGGER_SETTINGS = {
    'SECURITY_DEFINITIONS': {
        'Basic': {
            'type': 'basic'
        },
        'Bearer': {
            'type': 'apiKey',
            'name': 'Authorization',
            'in': 'header'
        }
    }
}

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.OrderingFilter',
        'rest_framework.filters.SearchFilter',
    ),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=1),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": False,
    "UPDATE_LAST_LOGIN": False,

    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "VERIFYING_KEY": "",
    "AUDIENCE": None,
    "ISSUER": None,
    "JSON_ENCODER": None,
    "JWK_URL": None,
    "LEEWAY": 0,

    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_HEADER_NAME": "HTTP_AUTHORIZATION",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "USER_AUTHENTICATION_RULE": "rest_framework_simplejwt.authentication.default_user_authentication_rule",

    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "TOKEN_TYPE_CLAIM": "token_type",
    "TOKEN_USER_CLASS": "rest_framework_simplejwt.models.TokenUser",

    "JTI_CLAIM": "jti",

    "SLIDING_TOKEN_REFRESH_EXP_CLAIM": "refresh_exp",
    "SLIDING_TOKEN_LIFETIME": timedelta(minutes=5),
    "SLIDING_TOKEN_REFRESH_LIFETIME": timedelta(days=1),

    "TOKEN_OBTAIN_SERIALIZER": "rest_framework_simplejwt.serializers.TokenObtainPairSerializer",
    "TOKEN_REFRESH_SERIALIZER": "rest_framework_simplejwt.serializers.TokenRefreshSerializer",
    "TOKEN_VERIFY_SERIALIZER": "rest_framework_simplejwt.serializers.TokenVerifySerializer",
    "TOKEN_BLACKLIST_SERIALIZER": "rest_framework_simplejwt.serializers.TokenBlacklistSerializer",
    "SLIDING_TOKEN_OBTAIN_SERIALIZER": "rest_framework_simplejwt.serializers.TokenObtainSlidingSerializer",
    "SLIDING_TOKEN_REFRESH_SERIALIZER": "rest_framework_simplejwt.serializers.TokenRefreshSlidingSerializer",
}

JAZZMIN_SETTINGS = {
    "show_ui_builder": True,
    "theme": 'default',
    "site_title": "Smart Fleet Admin Panel",
    "site_header": "Smart Fleet",
    "welcome_sign": "Welcome to Smart Fleet Admin Panel",
}

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
]

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_HEADERS = ['*']

DATA_UPLOAD_MAX_MEMORY_SIZE = 104857600

AWS_ACCESS_KEY_ID = config('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = config('AWS_SECRET_ACCESS_KEY')
AWS_STORAGE_BUCKET_NAME = config('AWS_STORAGE_BUCKET_NAME')
AWS_S3_REGION_NAME = config('AWS_S3_REGION_NAME')
AWS_S3_SIGNATURE_NAME = config('AWS_S3_SIGNATURE_NAME')
AWS_S3_FILE_OVERWRITE = False
AWS_DEFAULT_ACL = None
# storage set up for media and static
AWS_S3_CUSTOM_DOMAIN = f'{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_S3_REGION_NAME}.amazonaws.com'
AWS_S3_OBJECT_PARAMETERS = {
    'CacheControl': 'max-age=86400',  # 1 day cache
}

# Static files (CSS, JavaScript, Images)
STATIC_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/static/'

# Media files (Uploads)
MEDIA_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/'

# Configure Django to use the custom storage backends
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
    },
    "staticfiles": {
        "BACKEND": "storages.backends.s3boto3.S3StaticStorage",
    },
}

BOT_TOKEN = config('BOT_TOKEN')
DEVELOPER_CHAT_ID = config('DEVELOPER_CHAT_ID')
RATE_CON_CHAT_ID = config('RATE_CON_CHAT_ID')
SPACELINE_CLIENT = config('SPACELINE_CLIENT')
SPACELINE_PASSWORD = config('SPACELINE_PASSWORD')
PRIORITY_CLIENT = config('PRIORITY_CLIENT')
PRIORITY_PASSWORD = config('PRIORITY_PASSWORD')
ROADPULSE_CLIENT = config('ROADPULSE_CLIENT')
ROADPULSE_PASSWORD = config('ROADPULSE_PASSWORD')
TPA_CLIENT = config('TPA_CLIENT')
TPA_PASSWORD = config('TPA_PASSWORD')
RTS_HOST = config('RTS_HOST')
RTS_PORT = config('RTS_PORT')
RAHMATOLLOH_TG_ID=config('RAHMATOLLOH_TG_ID')
AI_ACCESS_TOKEN=config('AI_ACCESS_TOKEN')
CREATED_LOADS=config('CREATED_LOADS')
RTS_UPLOAD_GROUP=config('RTS_UPLOAD_GROUP')
PC_MILER_URL=config('PC_MILER_URL')
PC_MILER_KEY=config('PC_MILER_KEY')
PC_MILER_ROUTE_PATH_URL=config('PC_MILER_ROUTE_PATH_URL')
PC_MILER_ROUTE_REPORTS_URL=config('PC_MILER_ROUTE_REPORTS_URL')
GEMINI_API_KEY=config('GEMINI_API_KEY')
TELNYX_API_KEY=config('TELNYX_API_KEY')
TELNYX_PHONE_NUMBER=config('TELNYX_PHONE_NUMBER')
GOOGLE_MAPS_API_KEY=config('GOOGLE_API_KEY')
TEST_PHONE_NUMBER = "+1(628)2989349"
TEST_OTP_CODE = "111111"

# celery
CELERY_BROKER_URL = 'redis://localhost:6379/0'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'

# email
EMAIL_BACKEND = 'anymail.backends.mailgun.EmailBackend'
ANYMAIL = {
    'MAILGUN_API_KEY': config('MAILGUN_API_KEY'),
    'MAILGUN_SENDER_DOMAIN': config('MAILGUN_DOMAIN'),
}

DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', 'noreply@boxmanage.smartfleetllc.com')

# assistant
INTERNAL_SERVICE_SECRET = config('INTERNAL_SERVICE_SECRET')

# atrek (Go loads/bids service) — for the handful of calls boxTruck makes
# outbound to it, e.g. telling its broker cache to refresh immediately
# after a broker is created/updated here, instead of it finding out on its
# own next 30s poll.
ATREK_BASE_URL = config('ATREK_BASE_URL', default='https://spaceline.boxtruckmanage.com/api/v1')
