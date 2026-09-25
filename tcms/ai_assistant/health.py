"""存活与就绪探针。

把"进程还活着"和"依赖可用"分开表达，故障时才能一眼看出是哪一层出的问题：

* ``/health/``  存活探针。只证明进程能响应 HTTP，**不访问数据库**。
* ``/ready/``   就绪探针。额外校验数据库连通性与迁移完整性，未就绪返回 503。

两个路径都必须在 ``CheckDBStructureExistsMiddleware`` 的豁免名单里，否则数据
库一断，存活探针会被重定向到 /init-db/ 而被误判为不健康——这正是原来直接探测
``/accounts/login/`` 时的症状。

就绪探测被放进带超时的独立线程，原因是实测数据：数据库容器停止后，Docker 内置
DNS 解析主机名要 8 秒才报错，驱动层再加上重试，单次连接阻塞 32 秒，而 uwsgi 的
``harakiri`` 是 30 秒——结果不是返回 503，而是 worker 被 SIGKILL、nginx 回 502，
并且被卡住的 worker 会连带让整个站点不可用。探测本身绝不能有这种杀伤力。
"""

import os
import threading
import time

from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


SERVICE_NAME = "kiwi-tcms-ai"
CACHE_PROBE_KEY = "ai-health-probe"
DEFAULT_READINESS_TIMEOUT = 3.0

# 进程启动时刻，暴露 uptime 便于区分"持续运行"与"刚刚重启过"
_STARTED_AT = time.monotonic()


def _describe(exc):
    return f"{type(exc).__name__}: {exc}"


def _readiness_timeout():
    """单次就绪探测允许占用的秒数，可用 KIWI_READINESS_TIMEOUT 覆盖。"""
    try:
        value = float(os.environ.get("KIWI_READINESS_TIMEOUT", DEFAULT_READINESS_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_READINESS_TIMEOUT
    return value if value > 0 else DEFAULT_READINESS_TIMEOUT


def _check_database():
    """真实执行一次 SELECT 1，而不是只看连接对象是否构造成功。"""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # pylint: disable=broad-except
        return False, _describe(exc)
    return True, None


def _check_migrations():
    """比对磁盘迁移与数据库记录，部署漏跑迁移时立刻暴露。"""
    try:
        executor = MigrationExecutor(connection)
        pending_plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception as exc:  # pylint: disable=broad-except
        return False, _describe(exc), []

    if not pending_plan:
        return True, None, []

    pending = sorted(f"{migration.app_label}.{migration.name}" for migration, _ in pending_plan)
    return False, f"{len(pending)} 个迁移未应用", pending


def _check_cache():
    """缓存不是关键依赖：出问题只上报，不把整个服务判为未就绪。"""
    try:
        cache.set(CACHE_PROBE_KEY, "1", 5)
        value = cache.get(CACHE_PROBE_KEY)
        cache.delete(CACHE_PROBE_KEY)
    except Exception as exc:  # pylint: disable=broad-except
        return False, _describe(exc)

    if value != "1":
        return False, "缓存写入后读回失败（可能配置为 DummyCache）"
    return True, None


def _collect_database_state():
    """数据库 + 迁移的完整状态，作为整体放进带超时的线程里执行。"""
    database_ok, database_error = _check_database()
    if not database_ok:
        return database_ok, database_error, False, "数据库不可用，已跳过", []

    migrations_ok, migrations_error, pending = _check_migrations()
    return database_ok, database_error, migrations_ok, migrations_error, pending


def _run_bounded(func, timeout):
    """在独立线程里执行 func，最多等待 timeout 秒。

    返回 ``("ok", 结果)`` / ``("timeout", None)`` / ``("error", 异常)``。
    超时后不再等待：调用方必须立刻给出结论，把工作进程让出来。
    """
    outcome = {}

    def target():
        try:
            outcome["value"] = func()
        except Exception as exc:  # pylint: disable=broad-except
            outcome["error"] = exc
        finally:
            # 该线程第一次访问数据库会创建线程私有的连接，必须显式关闭，
            # 否则每次探测都会占用一个数据库连接直到线程对象被回收
            connection.close()

    thread = threading.Thread(target=target, name="ai-readiness-probe", daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        return "timeout", None
    if "error" in outcome:
        return "error", outcome["error"]
    return "ok", outcome.get("value")


def _unavailable(reason, skipped):
    return (
        {"ok": False, "error": reason},
        {"ok": False, "error": skipped, "pending": []},
    )


@never_cache
@require_GET
def health(_request):
    """存活探针：不触碰数据库，任何依赖故障都不应影响它。"""
    return JsonResponse(
        {
            "status": "ok",
            "service": SERVICE_NAME,
            "uptime_seconds": round(time.monotonic() - _STARTED_AT, 1),
        },
        json_dumps_params={"ensure_ascii": False},
    )


@never_cache
@require_GET
def ready(_request):
    """就绪探针：数据库可用且迁移完整才算 ready。"""
    timeout = _readiness_timeout()
    status, state = _run_bounded(_collect_database_state, timeout)

    if status == "timeout":
        database, migrations = _unavailable(
            f"探测在 {timeout:g} 秒内未返回，数据库不可达或响应过慢", "探测超时，已跳过"
        )
    elif status == "error":
        database, migrations = _unavailable(_describe(state), "探测异常，已跳过")
    else:
        database_ok, database_error, migrations_ok, migrations_error, pending = state
        database = {"ok": database_ok, "error": database_error}
        migrations = {"ok": migrations_ok, "error": migrations_error, "pending": pending}

    cache_ok, cache_error = _check_cache()

    checks = {
        "database": database,
        "migrations": migrations,
        "cache": {"ok": cache_ok, "error": cache_error, "critical": False},
    }

    is_ready = checks["database"]["ok"] and checks["migrations"]["ok"]
    return JsonResponse(
        {
            "status": "ready" if is_ready else "not_ready",
            "service": SERVICE_NAME,
            "checks": checks,
        },
        status=200 if is_ready else 503,
        # 出错原因要能在 docker logs / 终端里直接读，不要转成 \uXXXX
        json_dumps_params={"ensure_ascii": False},
    )
