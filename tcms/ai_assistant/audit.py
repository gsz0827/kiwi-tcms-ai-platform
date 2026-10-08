"""操作审计：谁在什么时候对什么对象做了什么、结果如何。

与 ``django-simple-history`` 的分工
-----------------------------------
两者互补，缺一不可：

* ``simple_history`` 记录「模型字段被改动了」，回答「发布门禁的最低成功率从
  95 改成 90，是谁改的、什么时候改的」；
* 本模块记录「动作级事件」，覆盖**不产生字段变更**的动作（审批被驳回、越权被
  拒、导出报告）以及请求来源 IP、request id、给出的理由——内审追问时这些同样
  是必答项。

因此模型改动走 ``KiwiHistoricalRecords``，业务动作走 ``record()``，两者都查得到
才算闭环。

留痕不可篡改
------------
审计表只增不改不删：``AIAuditLog.save()`` 拒绝更新已有行，``delete()`` 直接抛
异常，后台只提供查询。数据库账号层面的收敛（收回 ``UPDATE`` / ``DELETE``）属于
部署动作，见 ``docs/ai-platform-operations.md``。

写入失败不阻断业务
------------------
``record()`` 只负责落一行记录，任何异常都不允许影响主流程——审计写不进去不应该
让一次正常的审批变成 500。所以它吞掉写入异常并记一条 error 日志；日志里带
``request_id``，便于事后按 id 补录或排查。
"""

import logging
import uuid

from django.db import transaction

from .models import AIAuditLog


logger = logging.getLogger(__name__)


# 动作清单的唯一来源是模型上的 choices：后台筛选、迁移与文档都跟着它走。
AUDIT_ACTIONS = dict(AIAuditLog.ACTION_CHOICES)

RESULT_SUCCESS = "success"
RESULT_DENIED = "denied"
RESULT_FAILED = "failed"


def is_known_action(action):
    return action in AUDIT_ACTIONS


def _target_parts(target):
    """从对象或 ``(类型, 主键)`` 二元组里取出类型、主键与展示名。"""
    if target is None:
        return "", "", ""
    if isinstance(target, (tuple, list)) and len(target) == 2:
        return str(target[0]), str(target[1]), ""
    kind = target._meta.label
    return kind, str(target.pk or ""), str(target)[:255]


def client_ip(request):
    """取请求来源 IP。

    只使用服务器提供的 ``REMOTE_ADDR``。当前 Nginx 通过 uWSGI 参数传递
    连接来源地址；忽略客户端可伪造的 ``X-Real-IP`` / ``X-Forwarded-For``。
    若以后增加反向代理，需先明确可信代理边界，不能直接信任任意请求头。
    """
    if request is None:
        return None
    # uWSGI REMOTE_ADDR is set by Nginx; client-supplied forwarding headers are not trusted.
    value = request.META.get("REMOTE_ADDR") or ""
    value = value.strip()
    return value[:45] or None


def roles_snapshot(user):
    """操作时的角色快照，用于回答「他以什么身份做的这件事」。"""
    if user is None or not getattr(user, "is_authenticated", False):
        return ""
    try:
        from .roles import roles_of

        return "、".join(sorted(roles_of(user)))[:64]
    except Exception:  # pylint: disable=broad-except
        # 角色查不到不能影响审计落库，留空即可。
        return ""


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def changed_fields(before, after, names):
    """对比两个模型实例的指定字段，返回 ``{字段: {"from": 旧, "to": 新}}``。"""
    if before is None or after is None:
        return None
    return changed_mapping(
        {name: getattr(before, name, None) for name in names},
        {name: getattr(after, name, None) for name in names},
    )


def changed_mapping(before, after):
    """对比两份普通字典，返回 ``{字段: {"from": 旧值, "to": 新值}}``。

    表单保存前先把旧值抓成字典（实例会被原地改掉），保存后再抓一次，用这个函数
    生成变更明细。没有变化时返回 ``None``，避免在审计里塞一堆空对象。
    """
    if before is None or after is None:
        return None
    diff = {}
    for name, old in before.items():
        new = after.get(name)
        if old != new:
            diff[name] = {"from": _jsonable(old), "to": _jsonable(new)}
    return diff or None


def denied(request, attempted, reason, *, target=None, product=None, detail=None):
    """记录一次被拒绝的操作尝试。

    越权尝试本身就是审计要看的信号——「谁试图做什么但没被允许」比「他成功了什么」
    更能说明问题。视图里 ``raise PermissionDenied`` 之前调一次即可。
    """
    payload = {"attempted": attempted}
    if detail:
        payload.update(detail)
    record(
        request,
        "permission_denied",
        target=target,
        result=RESULT_DENIED,
        reason=reason,
        detail=payload,
        product=product,
    )


def record(
    request,
    action,
    *,
    target=None,
    target_repr="",
    result=RESULT_SUCCESS,
    reason="",
    detail=None,
    product=None,
    actor=None,
):
    """写入一条审计记录。

    ``action`` 应当取自 :data:`AUDIT_ACTIONS`；未登记的动作仍会落库，但会打一条
    warning，避免新动作悄悄绕开中文标签与文档。

    ``target`` 可以是模型实例，也可以是 ``(类型, 主键)`` 二元组。

    ``reason`` 单独成列：风险放行、驳回这类场景的理由是审计最关心的内容，抽出来
    便于检索，不必钻进 JSON。
    """
    if actor is None:
        actor = getattr(request, "user", None)
    if actor is not None and not getattr(actor, "is_authenticated", False):
        actor = None
    if not is_known_action(action):
        logger.warning("audit action %s is not registered in AIAuditLog.ACTION_CHOICES", action)

    request_id = getattr(request, "request_id", "") if request else ""
    if request is not None and not request_id:
        request_id = uuid.uuid4().hex
        request.request_id = request_id
    kind, pk, label = _target_parts(target)
    if target_repr:
        label = str(target_repr)[:255]

    try:
        # A nested savepoint keeps a failed journal insert from poisoning an outer transaction.
        with transaction.atomic():
            AIAuditLog.objects.create(
                actor=actor,
                actor_username=getattr(actor, "username", "") if actor else "",
                actor_role=roles_snapshot(actor),
                action=action,
                result=result,
                target_kind=kind,
                target_id=pk,
                target_repr=label,
                reason=(reason or "")[:2000],
                detail=dict(detail) if detail else {},
                product=product,
                ip=client_ip(request),
                request_id=request_id,
            )
        logger.info("audit action=%s target=%s:%s request_id=%s", action, kind, pk, request_id)
    except Exception:  # pylint: disable=broad-except
        # 审计写不进去不能改变业务结果；日志带 request_id，事后可按 id 补录。
        logger.exception("audit write failed action=%s target=%s:%s request_id=%s", action, kind, pk, request_id)
