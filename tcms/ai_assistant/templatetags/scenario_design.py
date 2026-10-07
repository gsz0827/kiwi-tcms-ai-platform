from django import template
from ..scenario_design import parse_design, inline_test_data

register = template.Library()


@register.inclusion_tag("ai_assistant/_scenario_design.html")
def scenario_design(case, show_metadata=True):
    return {"case": case, "design": inline_test_data(parse_design(case.text)), "show_metadata": show_metadata}
