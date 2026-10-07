"""Structured document fields; preserve legacy text and compose complete AI context."""

import re

REQUIREMENT_SECTIONS = (
    ("background", "背景与目标", False, "说明为什么做、解决什么问题、面向哪些用户"),
    ("impact_scope", "影响范围", False, "涉及菜单 / 页面 / 功能 / 接口，以及本次不包含的内容"),
    ("business_rules", "业务规则", False, "1. 输入限制与权限规则\n2. 计算规则与状态变化"),
    ("exceptions", "异常处理", False, "1. 失败时的提示与处理\n2. 超时、重复操作等情况下的行为"),
    (
        "acceptance_criteria",
        "验收标准",
        False,
        "1. 可验证的完成条件\n2. 正常、异常及边界条件下的预期行为",
    ),
    ("nonfunctional", "性能、安全与兼容要求", True, "按需填写响应时间、数据安全、兼容范围等要求"),
    ("references", "原型与参考资料", True, "原型地址、参考文档及补充说明"),
)
TASK_SECTIONS = (
    ("objective", "任务目标", False, "本任务需要实现什么、交付什么"),
    ("impact_scope", "影响范围", False, "实际改动的菜单 / 页面 / 服务 / 接口 / 数据"),
    ("business_rules", "规则实现", False, "对应需求中的业务规则如何实现"),
    ("exceptions", "异常处理", False, "异常处理机制、错误码及兼容影响"),
    ("interfaces", "接口说明", True, "接口地址、请求参数、响应与错误码"),
    ("data_changes", "数据变更", True, "表结构、字段、数据迁移及兼容影响"),
    ("deployment", "部署与回退说明", True, "配置变更、部署步骤、回退方式"),
    ("test_notes", "测试注意事项", True, "测试环境、准备数据、影响范围及重点验证项"),
    ("references", "参考资料", True, "关联文档、代码变更或其他参考地址"),
)
LIST_FIELDS = {"business_rules", "exceptions", "acceptance_criteria", "acceptance"}


def item_markdown(value):
    """One plain line per criterion; preserve explicit Markdown blocks and lists."""
    value = str(value or "")
    lines = value.splitlines()
    if any(re.match(r"^\s*(?:#{1,6}\s|[-+*]\s|\d+[.)]\s|[>|]|```|~~~)", line) for line in lines):
        return value
    return "\n".join(
        f"{index}. {line.strip()}"
        for index, line in enumerate((line for line in lines if line.strip()), 1)
    )


def blocks(document, task=False, include_empty=True):
    """Keep core requirement sections visible; AI context omits empty sections."""
    values = document.document_sections or {}
    definitions = TASK_SECTIONS if task else REQUIREMENT_SECTIONS
    rows = []
    for name, label, optional, _ in definitions:
        # The existing body is never parsed or split heuristically.
        if name == "business_rules":
            body_name = "description" if task else "requirement"
            rows.append(
                {
                    "name": body_name,
                    "label": "实现方案与改动说明" if task else "功能说明",
                    "body": getattr(document, body_name) or "暂无内容",
                    "optional": False,
                }
            )
        value = values.get(name, "")
        if value or (include_empty and not task and not optional):
            rows.append(
                {
                    "name": name,
                    "label": label,
                    "body": item_markdown(value) if name in LIST_FIELDS else value,
                    "optional": optional,
                    "empty": not str(value or "").strip(),
                }
            )
        if task and name == "exceptions" and document.acceptance:
            rows.append(
                {
                    "name": "acceptance",
                    "label": "验收标准",
                    "body": item_markdown(document.acceptance),
                    "optional": False,
                }
            )
    return rows


def requirement_text(document):
    if not document.document_sections and not document.target_version_id:
        return document.requirement
    rows = [f'## {row["label"]}\n\n{row["body"]}' for row in blocks(document, include_empty=False)]
    if document.target_version_id:
        rows.insert(0, f"目标项目版本：{document.target_version.value}")
    return "\n\n".join(rows)


def requirement_attributes(document):
    return {
        "target_version_id": document.target_version_id,
        "target_version": document.target_version.value if document.target_version_id else "",
        "assigned_to_id": document.assigned_to_id,
        "assigned_to": document.assigned_to.username if document.assigned_to_id else "",
        "priority": document.priority,
        "status": document.status,
    }
