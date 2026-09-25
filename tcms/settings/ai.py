"""Settings for the repository's standalone AI deployment."""

import os

from django.core.exceptions import ImproperlyConfigured

from .product import *  # noqa: F403

SECRET_KEY = os.environ.get("KIWI_SECRET_KEY", "")
if len(SECRET_KEY) < 50:
    raise ImproperlyConfigured("KIWI_SECRET_KEY must contain at least 50 characters")

DEBUG = False
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("KIWI_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.environ.get("KIWI_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
ANONYMOUS_ANALYTICS = False
API_AUTOMATION_ALLOWED_ORIGINS = [
    item.strip() for item in os.environ.get(
        "KIWI_API_ALLOWED_ORIGINS", "http://api-demo:8080"
    ).split(",") if item.strip()
]


# ---------------------------------------------------------------------------
# 可观测性
#
# 上游日志配置（tcms.settings.common）面向的是带发信能力的经典部署：ERROR 交给
# AdminEmailHandler，而本部署容器里没有 SMTP，邮件发送失败会再记一条 ERROR，
# 只剩噪音。这里改为统一输出到 stdout，并让每行日志带上 request id。
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("KIWI_LOG_LEVEL", "INFO").upper()

# 必须排在最前：后续中间件与视图里打的日志都要能取到 request id
MIDDLEWARE = [  # noqa: F405
    "tcms.ai_assistant.observability.RequestIDMiddleware",
    *MIDDLEWARE,  # noqa: F405
]

# 数据库不可达时，驱动层的 DNS 解析加连接重试实测会阻塞 30 秒以上，足以触发
# uwsgi 的 harakiri（30 秒）把 worker 杀掉。给连接建立加上明确上限，让故障快速
# 暴露，而不是把工作进程拖死。
DATABASES["default"].setdefault("OPTIONS", {})["connect_timeout"] = int(  # noqa: F405
    os.environ.get("KIWI_DB_CONNECT_TIMEOUT", "5")
)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "tcms.ai_assistant.observability.RequestIDLogFilter"},
    },
    "formatters": {
        "structured": {
            "format": "[%(asctime)s] %(levelname)s request_id=%(request_id)s "
            "%(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
            "filters": ["request_id"],
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        # 请求级访问日志，由 RequestIDMiddleware 记录
        "tcms.request": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
