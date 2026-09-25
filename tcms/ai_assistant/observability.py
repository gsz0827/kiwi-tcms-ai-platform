"""请求标识与访问日志。

容器日志是这套环境唯一的排查入口，所以这里做两件事：

* 给每个请求分配 request id（优先复用反向代理传入的 X-Request-ID），并以响应头
  ``X-Request-ID`` 回写，方便把浏览器里看到的报错与后端日志对上。
* 让所有日志行自动带上 request id，并为每个请求记录一条结构化访问日志
  （方法、路径、状态码、耗时）。

该中间件必须排在 ``MIDDLEWARE`` 最前面，让后续中间件与视图的日志都能带上标识。
"""

import contextvars
import logging
import re
import time
import uuid

from django.utils.deprecation import MiddlewareMixin


REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_META_KEY = "HTTP_X_REQUEST_ID"
REQUEST_ID_ENVIRON_KEY = "kiwi.request_id"

ACCESS_LOGGER_NAME = "tcms.request"

# 只接受形状可控的标识，避免把外部任意字符串（含换行）直接写进日志
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")

_request_id_var = contextvars.ContextVar("kiwi_request_id", default="-")


def get_request_id():
    """返回当前请求的标识；不在请求上下文中时返回 ``-``。"""
    return _request_id_var.get()


def _incoming_request_id(request):
    candidate = request.META.get(REQUEST_ID_META_KEY, "")
    if candidate and _SAFE_REQUEST_ID.match(candidate):
        return candidate
    return None


class RequestIDMiddleware(MiddlewareMixin):
    def process_request(self, request):
        request.request_id = _incoming_request_id(request) or uuid.uuid4().hex
        request.META[REQUEST_ID_ENVIRON_KEY] = request.request_id
        request.request_id_token = _request_id_var.set(request.request_id)
        request.request_started_at = time.monotonic()
        return None

    def process_response(self, request, response):
        request_id = getattr(request, "request_id", "")
        if not request_id:
            return response

        response.headers[REQUEST_ID_HEADER] = request_id

        started_at = getattr(request, "request_started_at", None)
        duration = time.monotonic() - started_at if started_at else 0.0
        logging.getLogger(ACCESS_LOGGER_NAME).info(
            "method=%s path=%s status=%s duration=%.3fs",
            request.method,
            request.path,
            response.status_code,
            duration,
        )

        # 还原上下文变量，避免请求结束后残留的标识污染请求之外打出的日志
        token = getattr(request, "request_id_token", None)
        if token is not None:
            _request_id_var.reset(token)
        return response


class RequestIDLogFilter(logging.Filter):
    """把当前请求的 request id 注入日志记录，供 formatter 通过 %(request_id)s 引用。"""

    def filter(self, record):
        record.request_id = get_request_id()
        return True
