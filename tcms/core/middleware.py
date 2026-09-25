# pylint: disable=no-self-use, too-few-public-methods

from django.conf import settings
from django.contrib.sites.models import Site
from django.db.utils import OperationalError, ProgrammingError
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.deprecation import MiddlewareMixin


# 这些路径不参与"数据库结构是否就绪"的判断：
#   /init-db/  本身就是用来建库的入口；
#   /health/   存活探针，必须能在数据库不可用时正常响应，否则会被 302 到
#              /init-db/ 而被误判为进程不健康；
#   /ready/    就绪探针，需要自己返回 503 而不是被重定向。
DB_STRUCTURE_EXEMPT_PATHS = ("/init-db", "/health", "/ready")


class CheckDBStructureExistsMiddleware(MiddlewareMixin):
    def process_request(self, request):
        if request.path.rstrip("/") in DB_STRUCTURE_EXEMPT_PATHS:
            return None
        try:
            Site.objects.get(pk=settings.SITE_ID)
        except (OperationalError, ProgrammingError):
            # Redirect to Setup view
            return HttpResponseRedirect(reverse("init-db"))
        return None


class ExtraHeadersMiddleware(MiddlewareMixin):
    """
    This is enabled only during testing and development. The actual headers
    are configured in `etc/nginx.conf`!
    """

    def process_response(self, request, response):
        if settings.DEBUG:
            response.headers["Content-Security-Policy"] = (
                "script-src 'self' cdn.crowdin.com *.ethicalads.io plausible.io;"
            )

            if request.path.find("/uploads/") > -1:
                response.headers["Content-Type"] = "text/plain"

        return response
