"""Loss-aware presentation of native case text; never rewrite cases while reading."""

import re
from dataclasses import dataclass, field, replace

SECTIONS = {
    "前置条件": "preconditions",
    "测试数据": "test_data",
    "测试步骤": "steps",
    "步骤": "steps",
    "补充原文": "extra",
}
BLOCK_FIELDS = ("preconditions", "test_data", "extra")


@dataclass
class CaseDesign:
    test_type: str = ""
    case_number: str = ""
    preconditions: str = ""
    test_data: str = ""
    extra: str = ""
    steps: list = field(default_factory=list)

    @property
    def structured(self):
        return bool(self.steps)


def parse_design(text):
    design = CaseDesign()
    blocks = {key: [] for key in BLOCK_FIELDS}
    section, current, part = "extra", None, "action"
    for line in (text or "").replace("\r\n", "\n").splitlines():
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading:
            title = heading[1].strip()
            section = SECTIONS.get(title, "extra")
            current = None
            if title not in SECTIONS:
                blocks["extra"].append(line)
            continue
        metadata = re.match(r"^\*\*(AI 用例编号|测试类型)[：:]\s*\*\*\s*(.*)$", line)
        if metadata:
            key = "case_number" if metadata[1] == "AI 用例编号" else "test_type"
            if getattr(design, key):
                blocks["extra"].append(line)
            else:
                setattr(design, key, metadata[2])
            continue
        # Provenance notes and unknown sections remain visible, never discarded.
        if line.startswith(">"):
            blocks["extra"].append(line)
            continue
        if section != "steps":
            blocks[section].append(line[2:] if line.startswith("- ") else line)
            continue
        numbered = re.match(r"^ {0,2}\d+[.)、]\s+(.*)$", line)
        detail = re.match(r"^\s*-\s*(预期(?:结果)?|数据|测试数据)[：:]\s?(.*)$", line)
        if numbered:
            current = {"action": numbered[1], "data": "", "expected": ""}
            design.steps.append(current)
            part = "action"
        elif detail and current:
            part = "expected" if detail[1].startswith("预期") else "data"
            current[part] += ("\n" if current[part] else "") + detail[2]
        elif current:
            # Serializer uses a fixed continuation indent; retain internal whitespace.
            continuation = (
                line[5:] if line.startswith("     ") else line[3:] if line.startswith("   ") else line
            )
            current[part] += "\n" + continuation
        else:
            blocks["extra"].append(line)
    for key, lines in blocks.items():
        setattr(design, key, "\n".join(lines).strip("\n"))
    for step in design.steps:
        for key in step:
            step[key] = step[key].rstrip("\n")
    return design


def inline_test_data(design):
    """Present legacy common data in the first input cell without rewriting source text."""
    if not design.test_data:
        return design
    steps = [dict(step) for step in design.steps] or [{"action": "", "data": "", "expected": ""}]
    existing = steps[0].get("data", "")
    steps[0]["data"] = design.test_data if not existing or existing == design.test_data else f"{design.test_data}\n\n{existing}"
    return replace(design, test_data="", steps=steps)


def serialize_design(design):
    lines = []
    if design.case_number:
        lines.append(f"**AI 用例编号：** {design.case_number}")
    if design.test_type:
        lines.append(f"**测试类型：** {design.test_type}")
    for title, key in (("前置条件", "preconditions"), ("测试数据", "test_data")):
        if getattr(design, key):
            lines.extend(["", f"### {title}"])
            lines.extend("- " + item for item in getattr(design, key).splitlines())
    lines.extend(["", "### 测试步骤"])
    for index, step in enumerate(design.steps, 1):
        action = step["action"].splitlines()
        lines.append(f"{index}. {action[0]}")
        lines.extend("   " + item for item in action[1:])
        for key, label in (("data", "数据"), ("expected", "预期")):
            if step.get(key):
                value = step[key].splitlines()
                lines.append(f"   - {label}：{value[0]}")
                lines.extend("     " + item for item in value[1:])
    for title, key in (("补充原文", "extra"),):
        if getattr(design, key):
            lines.extend(["", f"### {title}"])
            lines.extend("- " + item for item in getattr(design, key).splitlines())
    return "\n".join(lines).strip("\n")
