from django import template
from tcms.allure_reporting.views import context as report_context

register = template.Library()


@register.inclusion_tag("allure_reporting/card.html", takes_context=True)
def allure_report_card(context, kind, run):
    request = context["request"]
    data = report_context(request.user, kind, run)
    data["csrf_token"] = context.get("csrf_token")
    return data
