"""Business scenario read/edit rights; automation configuration remains owner-private."""

from guardian.shortcuts import get_objects_for_user
from tcms.testcases.models import TestCase
from .case_library import visible_cases
from . import roles


def editable_scenarios(user, product=None):
    if not user.is_active or roles.is_read_only(user):
        return TestCase.objects.none()
    cases = visible_cases(user).filter(
        pk__in=get_objects_for_user(user, "testcases.change_testcase", klass=TestCase).values("pk")
    )
    return cases.filter(category__product=product) if product is not None else cases


def can_edit_scenario(user, case):
    return editable_scenarios(user).filter(pk=case.pk).exists()
