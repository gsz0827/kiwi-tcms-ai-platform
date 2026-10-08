"""Deterministic quality counters, independent of AI analyses and report snapshots."""
from django.db.models import Count, Q
from guardian.shortcuts import get_objects_for_user

from tcms.testruns.models import TestExecution, TestRun
from tcms.web_testing.models import WebRun
from . import roles
from .models import APIRun


def visible_runs(user):
    if not user.is_authenticated or not user.is_active:
        return TestRun.objects.none()
    allowed = get_objects_for_user(user, "testruns.view_testrun", klass=TestRun)
    return TestRun.objects.filter(
        Q(pk__in=allowed) | Q(plan__product__in=roles.member_products(user))
    ).distinct()


def filtered_runs(user, product_id=None, version_id=None, plan_id=None):
    runs = visible_runs(user)
    if product_id:
        runs = runs.filter(plan__product_id=product_id)
    if version_id:
        runs = runs.filter(build__version_id=version_id)
    if plan_id:
        runs = runs.filter(plan_id=plan_id)
    return runs


def execution_metrics(user, product_id=None, version_id=None, plan_id=None):
    runs = filtered_runs(user, product_id, version_id, plan_id)
    counters = TestExecution.objects.filter(run__in=runs).aggregate(
        total=Count("pk"), passed=Count("pk", filter=Q(status__weight__gt=0)),
        failed=Count("pk", filter=Q(status__weight__lt=0)),
        pending=Count("pk", filter=Q(status__weight=0)),
    )
    executed = counters["passed"] + counters["failed"]
    counters.update(tasks=runs.count(), executed=executed,
                    pass_rate=round(counters["passed"] * 100 / executed, 1) if executed else None)
    # Personal diagnostic runs have no release-version/plan binding and must not
    # be mixed into formal results, or included in a version/plan-specific view.
    diagnostic = 0
    if not version_id and not plan_id:
        for model in (WebRun, APIRun):
            query = model.objects.filter(owner=user, test_run__isnull=True)
            if product_id:
                query = query.filter(product_id=product_id)
            diagnostic += query.count()
    counters["diagnostic_tasks"] = diagnostic
    return counters
