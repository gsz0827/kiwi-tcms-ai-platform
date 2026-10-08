"""Explicit presentation conversion also supports Kiwi's legacy naive UTC dates."""
from datetime import datetime
from django import template
from django.templatetags.tz import do_timezone

register = template.Library()


@register.filter
def in_display_zone(value, zone_name=None):
    if not isinstance(value, datetime):
        return value
    return do_timezone(value, zone_name or 'Asia/Shanghai')
