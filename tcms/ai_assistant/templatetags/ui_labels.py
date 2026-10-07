"""Presentation-only terminology; stored values and public API identifiers stay stable."""
from django import template

register = template.Library()


@register.filter(is_safe=True)
def ui_term(value):
    text = str(value)
    for old, new in (
        ('开发任务单', '开发任务'), ('任务单', '开发任务'),
        ('环境变量', '环境参数'), ('公共登录/前置用例', '前置执行脚本'),
        ('自动化配置', '自动化脚本'), ('测试运行', '执行任务'),
        ('回归验证', '缺陷复测'), ('回归通过', '复测通过'),
        ('回归失败', '复测失败'), ('回归未完成', '复测未完成'),
        ('回归测试运行', '复测执行任务'), ('回归执行任务', '复测执行任务'), ('产品', '项目'),
    ):
        text = text.replace(old, new)
    return text
