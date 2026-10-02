"""Bounded data tables shared by Web and API automation."""
import json
import math
import re
from django import forms


def variables(value):
    if not isinstance(value, dict) or len(value) > 50:
        raise ValueError('变量必须是最多 50 个字段的 JSON 对象。')
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', key):
            raise ValueError('变量名仅支持字母、数字和下划线，不能以数字开头。')
        if not isinstance(item, (str, int, float, bool)) or isinstance(item, float) and not math.isfinite(item):
            raise ValueError('变量值只能是文本、数字或布尔值。')
        if len(str(item)) > 2000:
            raise ValueError('单个变量值不能超过 2000 字符。')
    return value


def datasets(value):
    if value is None or value == []:
        return []
    if not isinstance(value, list) or len(value) > 10:
        raise ValueError('测试数据应是最多 10 组 JSON 对象组成的数组。')
    for row in value:
        variables(row)
    if len(json.dumps(value).encode()) > 65536:
        raise ValueError('测试数据总大小不能超过 64 KB。')
    return value


class DatasetField(forms.JSONField):
    def __init__(self, **kwargs):
        super().__init__(required=False, label='多组测试数据', widget=forms.Textarea(attrs={'rows':4}),
            help_text='例如 [{"user_id":1},{"user_id":2}]。每组完整运行一次所选用例，组间登录状态隔离；留空执行一次。数据加密保存。', **kwargs)

    def clean(self, value):
        try:
            return datasets(super().clean(value))
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
