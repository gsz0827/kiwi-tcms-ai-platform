"""Formal API CI submissions reuse browser validation and immutable bindings."""

import json
import uuid

from django.core.exceptions import ObjectDoesNotExist
from django.http import JsonResponse

from tcms.management.models import Build
from tcms.testcases.models import TestCase
from tcms.testplans.models import TestPlan
from .api_workflow import SuiteSubmitForm, create_run, submission_choices
from .models import APICase, APIEnvironment, APIRun
from .roles import is_read_only


def submit(request, suite):
    try:
        if len(request.body) > 4096:
            raise ValueError
        data = json.loads(request.body)
        if not isinstance(data, dict) or set(data) - {
            "execution_mode",
            "plan",
            "build",
            "environment",
        }:
            raise ValueError
        key = uuid.UUID(request.headers.get("Idempotency-Key", ""))
    except (ValueError, UnicodeError, RecursionError):
        return JsonResponse({"error": "需有效 UUID 提交标识及 JSON 执行配置。"}, status=400)
    if is_read_only(suite.owner):
        return JsonResponse({"error": "readonly_account"}, status=403)
    previous = APIRun.objects.filter(owner=suite.owner, submission_token=key).first()
    if previous:
        if previous.suite_id != suite.pk or previous.trigger != "ci":
            return JsonResponse({"error": "submission_key_conflict"}, status=409)
        return previous
    if (
        suite.runs.filter(status__in=APIRun.ACTIVE_STATUSES).exists()
        or APIRun.objects.filter(owner=suite.owner, status__in=APIRun.ACTIVE_STATUSES).count() >= 20
    ):
        return JsonResponse({"error": "套件已有活动任务或待执行任务过多。"}, status=409)
    data.setdefault("execution_mode", "debug")
    data.setdefault("environment", suite.environment_id)
    data["token"] = key

    # The caller already holds owner/suite locks, matching browser submissions.
    def identifier(value):
        value = str(value or "")
        return int(value) if value.isascii() and value.isdecimal() and len(value) <= 18 else None

    list(
        APIEnvironment.objects.select_for_update().filter(
            pk=identifier(data["environment"]), owner=suite.owner
        )
    )
    list(TestPlan.objects.select_for_update().filter(pk=identifier(data.get("plan"))))
    list(Build.objects.select_for_update().filter(pk=identifier(data.get("build"))))
    configs = list(
        APICase.objects.select_for_update().filter(owner=suite.owner, pk__in=suite.case_ids)
    )
    list(
        TestCase.objects.select_for_update().filter(
            pk__in=[item.test_case_id for item in configs if item.test_case_id]
        )
    )
    form = SuiteSubmitForm(data, owner=suite.owner, suite=suite)
    if not form.is_valid():
        return JsonResponse({"error": "执行配置无效，请核对计划、构建、环境及业务用例。"}, status=409)
    try:
        run = create_run(suite.owner, suite, form, submission_choices(data))
        run.trigger = "ci"
        run.save(update_fields=("trigger",))
        return run
    except (ValueError, RuntimeError, ObjectDoesNotExist):
        # Raise to roll back any partial formal task. The CI view translates it.
        raise ValueError("套件配置已变化，请在平台重新检查。") from None
