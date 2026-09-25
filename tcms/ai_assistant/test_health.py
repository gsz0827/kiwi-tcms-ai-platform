"""存活/就绪探针与请求标识的回归测试。

这两块此前完全缺失：容器健康检查打的是 ``/accounts/login/``，数据库一断就连带
被判为不健康，也分不清是进程挂了还是数据库断了；日志里没有任何请求标识，排查
只能靠时间戳猜。这里把行为钉死。
"""

import contextvars
import importlib
import logging
import os
import time
from unittest.mock import patch

from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from tcms.core.middleware import DB_STRUCTURE_EXEMPT_PATHS
from tcms.core.views import server_error

from .health import DEFAULT_READINESS_TIMEOUT, _check_migrations, _readiness_timeout
from .observability import (
    REQUEST_ID_HEADER,
    RequestIDLogFilter,
    RequestIDMiddleware,
    get_request_id,
)


class HealthRouteTests(SimpleTestCase):
    def test_health_and_ready_routes_are_registered(self):
        self.assertEqual(reverse("health"), "/health/")
        self.assertEqual(reverse("ready"), "/ready/")

    def test_probes_are_exempt_from_the_db_structure_check(self):
        """否则数据库一断，存活探针会被 302 到 /init-db/ 而误报。"""
        self.assertIn("/health", DB_STRUCTURE_EXEMPT_PATHS)
        self.assertIn("/ready", DB_STRUCTURE_EXEMPT_PATHS)


class HealthEndpointTests(TestCase):
    def test_health_does_not_touch_the_database(self):
        """存活探针必须与数据库解耦，0 次查询是这条约定的硬性证据。"""
        with self.assertNumQueries(0):
            response = self.client.get("/health/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "kiwi-tcms-ai")
        self.assertIn("uptime_seconds", payload)

    def test_health_accepts_path_without_trailing_slash(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_ready_accepts_path_without_trailing_slash(self):
        self.assertEqual(self.client.get("/ready").status_code, 200)

    def test_ready_reports_database_and_migrations(self):
        response = self.client.get("/ready/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["checks"]["database"]["ok"])
        self.assertTrue(payload["checks"]["migrations"]["ok"])
        self.assertEqual(payload["checks"]["migrations"]["pending"], [])
        # 缓存不是关键依赖，必须显式标注为不影响整体判定
        self.assertFalse(payload["checks"]["cache"]["critical"])

    def test_ready_is_503_when_the_database_is_unavailable(self):
        with patch(
            "tcms.ai_assistant.health._check_database",
            return_value=(False, "OperationalError: 连接被拒绝"),
        ):
            response = self.client.get("/ready/")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "not_ready")
        self.assertFalse(payload["checks"]["database"]["ok"])
        self.assertFalse(payload["checks"]["migrations"]["ok"])
        self.assertEqual(payload["checks"]["migrations"]["error"], "数据库不可用，已跳过")

    def test_write_methods_are_rejected(self):
        self.assertEqual(self.client.post("/health/").status_code, 405)
        self.assertEqual(self.client.post("/ready/").status_code, 405)


class ReadinessTimeoutTests(TestCase):
    """数据库不可达时，驱动层会阻塞 30 秒以上，必须由探测自己兜住。

    实测：停止数据库容器后，Docker 内置 DNS 解析要 8 秒才报错，驱动层加
    重试后单次连接阻塞 32 秒，而 uwsgi 的 harakiri 是 30 秒——结果是 worker
    被 SIGKILL、nginx 回 502，并且卡住的 worker 会让整个站点不可用。
    """

    @staticmethod
    def _slow_check():
        time.sleep(5)
        return True, None

    def test_slow_database_returns_503_without_blocking(self):
        started = time.monotonic()
        with patch("tcms.ai_assistant.health._check_database", self._slow_check), patch(
            "tcms.ai_assistant.health._readiness_timeout", return_value=0.2
        ):
            response = self.client.get("/ready/")
        elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 503)
        self.assertLess(elapsed, 2.0, "探测超时后必须立刻返回，不能等驱动层自己超时")

        payload = response.json()
        self.assertEqual(payload["status"], "not_ready")
        self.assertFalse(payload["checks"]["database"]["ok"])
        self.assertIn("秒内未返回", payload["checks"]["database"]["error"])
        self.assertEqual(payload["checks"]["migrations"]["error"], "探测超时，已跳过")

    def test_probe_exception_is_reported_not_raised(self):
        with patch("tcms.ai_assistant.health._collect_database_state") as collect:
            collect.side_effect = RuntimeError("意外崩溃")
            response = self.client.get("/ready/")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertIn("RuntimeError: 意外崩溃", payload["checks"]["database"]["error"])
        self.assertEqual(payload["checks"]["migrations"]["error"], "探测异常，已跳过")

    def test_timeout_falls_back_to_the_default_when_misconfigured(self):
        with patch.dict(os.environ, {"KIWI_READINESS_TIMEOUT": "not-a-number"}):
            self.assertEqual(_readiness_timeout(), DEFAULT_READINESS_TIMEOUT)
        with patch.dict(os.environ, {"KIWI_READINESS_TIMEOUT": "-1"}):
            self.assertEqual(_readiness_timeout(), DEFAULT_READINESS_TIMEOUT)
        with patch.dict(os.environ, {"KIWI_READINESS_TIMEOUT": "7.5"}):
            self.assertEqual(_readiness_timeout(), 7.5)


class FakeMigration:
    app_label = "ai_assistant"
    name = "0099_pending"


class FakeMigrationExecutor:
    """替身：让"有未应用迁移"的分支可以离线验证。"""

    def __init__(self, _connection):
        self.loader = self

    @property
    def graph(self):
        return self

    def leaf_nodes(self):
        return [("ai_assistant", "0099_pending")]

    def migration_plan(self, _targets):
        return [(FakeMigration(), False)]


class EmptyMigrationExecutor(FakeMigrationExecutor):
    def leaf_nodes(self):
        return []

    def migration_plan(self, _targets):
        return []


class MigrationCheckTests(SimpleTestCase):
    def test_pending_migrations_are_reported(self):
        with patch("tcms.ai_assistant.health.MigrationExecutor", FakeMigrationExecutor):
            ok, error, pending = _check_migrations()

        self.assertFalse(ok)
        self.assertEqual(pending, ["ai_assistant.0099_pending"])
        self.assertIn("1 个迁移未应用", error)

    def test_no_pending_migrations(self):
        with patch("tcms.ai_assistant.health.MigrationExecutor", EmptyMigrationExecutor):
            ok, error, pending = _check_migrations()

        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertEqual(pending, [])

    def test_broken_connection_is_reported_instead_of_raised(self):
        with patch("tcms.ai_assistant.health.MigrationExecutor", side_effect=RuntimeError("boom")):
            ok, error, pending = _check_migrations()

        self.assertFalse(ok)
        self.assertIn("RuntimeError: boom", error)
        self.assertEqual(pending, [])


class RequestIDMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @staticmethod
    def handler(_request):
        return HttpResponse("ok", status=201)

    def test_generates_a_request_id_when_the_client_sends_none(self):
        response = RequestIDMiddleware(self.handler)(self.factory.get("/ai/"))
        self.assertEqual(len(response[REQUEST_ID_HEADER]), 32)

    def test_reuses_the_client_supplied_request_id(self):
        request = self.factory.get("/ai/")
        request.META["HTTP_X_REQUEST_ID"] = "trace-0001"
        response = RequestIDMiddleware(self.handler)(request)
        self.assertEqual(response[REQUEST_ID_HEADER], "trace-0001")

    def test_untrusted_request_id_is_discarded(self):
        """外部标识会被写进日志，含换行的必须丢弃而不是原样透传。"""
        request = self.factory.get("/ai/")
        request.META["HTTP_X_REQUEST_ID"] = "forged\nINFO fake=line"
        response = RequestIDMiddleware(self.handler)(request)

        self.assertNotIn("\n", response[REQUEST_ID_HEADER])
        self.assertEqual(len(response[REQUEST_ID_HEADER]), 32)

    def test_access_log_records_method_path_status_and_duration(self):
        with self.assertLogs("tcms.request", level="INFO") as captured:
            RequestIDMiddleware(self.handler)(self.factory.get("/ai/api-testing/"))

        message = captured.records[0].getMessage()
        self.assertIn("method=GET", message)
        self.assertIn("path=/ai/api-testing/", message)
        self.assertIn("status=201", message)
        self.assertIn("duration=", message)

    def test_request_id_reaches_log_records(self):
        """视图里打的日志必须带上当前请求的标识，否则日志串不起来。"""
        seen = []

        def handler(_request):
            record = logging.LogRecord("tcms", logging.INFO, __file__, 1, "视图日志", None, None)
            RequestIDLogFilter().filter(record)
            seen.append(record.request_id)
            return HttpResponse("ok")

        response = RequestIDMiddleware(handler)(self.factory.get("/ai/"))

        self.assertEqual(seen, [response[REQUEST_ID_HEADER]])

    def test_request_id_does_not_leak_outside_the_request(self):
        """请求结束后残留的标识会污染同进程里其他日志，必须还原。"""
        middleware = RequestIDMiddleware(self.handler)
        middleware(self.factory.get("/ai/"))

        self.assertEqual(contextvars.Context().run(get_request_id), "-")


class RequestIDLogFilterTests(SimpleTestCase):
    def test_defaults_to_a_placeholder_outside_a_request(self):
        def build_record():
            record = logging.LogRecord("tcms", logging.INFO, __file__, 1, "无请求上下文", None, None)
            RequestIDLogFilter().filter(record)
            return record

        # 用空上下文求值，等价于"不在任何请求中"
        record = contextvars.Context().run(build_record)
        self.assertEqual(record.request_id, "-")

    def test_filter_never_drops_records(self):
        record = logging.LogRecord("tcms", logging.INFO, __file__, 1, "msg", None, None)
        self.assertTrue(RequestIDLogFilter().filter(record))


class AISettingsWiringTests(SimpleTestCase):
    """设置文件的改动没人验证就等于没改，这里把关键接线钉住。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch.dict(os.environ, {"KIWI_SECRET_KEY": "x" * 64}):
            cls.ai_settings = importlib.import_module("tcms.settings.ai")

    def test_request_id_middleware_is_first(self):
        expected = "tcms.ai_assistant.observability.RequestIDMiddleware"
        self.assertEqual(self.ai_settings.MIDDLEWARE[0], expected)
        self.assertEqual(self.ai_settings.MIDDLEWARE.count(expected), 1)

    def test_logging_uses_console_with_request_id_instead_of_email(self):
        logging_config = self.ai_settings.LOGGING
        self.assertIn("request_id", logging_config["filters"])
        self.assertEqual(
            logging_config["filters"]["request_id"]["()"],
            "tcms.ai_assistant.observability.RequestIDLogFilter",
        )
        self.assertNotIn("mail_admins", logging_config["handlers"])
        self.assertEqual(logging_config["loggers"]["django.request"]["handlers"], ["console"])
        self.assertIn("%(request_id)s", logging_config["formatters"]["structured"]["format"])

    def test_middleware_list_keeps_the_upstream_entries(self):
        self.assertIn("django.middleware.security.SecurityMiddleware", self.ai_settings.MIDDLEWARE)
        self.assertIn(
            "tcms.core.middleware.CheckDBStructureExistsMiddleware",
            self.ai_settings.MIDDLEWARE,
        )


class ServerErrorPageTests(SimpleTestCase):
    """500 页面必须带上请求编号。

    日志里已经有请求标识，但错误页原本以空上下文渲染，用户看到的只有一个 500，
    报障时无法给出可检索的线索，链路等于断的。
    """

    def test_page_shows_the_request_id_when_provided(self):
        html = render_to_string("500.html", {"request_id": "trace-abc123"})

        self.assertIn("trace-abc123", html)
        self.assertIn("请求编号", html)

    def test_page_omits_the_block_without_a_request_id(self):
        html = render_to_string("500.html", {})

        self.assertNotIn("请求编号", html)

    def test_server_error_renders_with_the_request_id_from_the_request(self):
        request = RequestFactory().get("/ai/boom/")
        request.request_id = "trace-xyz789"

        with patch("tcms.core.views.loader.get_template") as get_template:
            get_template.return_value.render.return_value = "<html></html>"
            response = server_error(request)

        self.assertEqual(response.status_code, 500)
        # 位置参数：context 在前，request 在后。
        context, passed_request = get_template.return_value.render.call_args[0][:2]
        self.assertEqual(context, {"request_id": "trace-xyz789"})
        self.assertIs(passed_request, request)

    def test_server_error_tolerates_a_request_without_an_identifier(self):
        """中间件未启用时不应报错，只是参数为空。"""
        with patch("tcms.core.views.loader.get_template") as get_template:
            get_template.return_value.render.return_value = "<html></html>"
            server_error(RequestFactory().get("/ai/boom/"))

        context = get_template.return_value.render.call_args[0][0]
        self.assertEqual(context, {"request_id": ""})

    def test_error_page_renders_before_authentication_middleware_runs(self):
        """数据库等前置中间件先失败时是真实场景。

        那时认证与会话中间件都还没执行，request 上没有 user/session。错误页必须
        仍能渲染出请求编号——否则用户连可以提供的线索都拿不到。
        """
        request = RequestFactory().get("/ai/boom/")
        request.request_id = "trace-no-auth"

        html = render_to_string("500.html", {"request_id": "trace-no-auth"}, request=request)

        self.assertIn("trace-no-auth", html)
