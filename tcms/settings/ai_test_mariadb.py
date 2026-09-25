"""Use only the disposable database in the test-mariadb Compose profile."""

import os

from .test import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": "kiwi_ai_test_base",
        "USER": "root",
        "PASSWORD": os.environ["KIWI_TEST_DB_PASSWORD"],
        "HOST": os.environ["KIWI_TEST_DB_HOST"],
        "PORT": "3306",
        "OPTIONS": {"charset": "utf8mb4", "init_command": "SET sql_mode='STRICT_TRANS_TABLES'"},
        "TEST": {"NAME": "test_kiwi_ai"},
    }
}
