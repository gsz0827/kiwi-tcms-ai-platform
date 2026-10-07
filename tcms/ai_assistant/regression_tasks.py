"""Create a fresh TCMS regression task; never execute it or close defects implicitly."""

import uuid
from collections import Counter

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from guardian.shortcuts import assign_perm, get_objects_for_user

from tcms.management.models import Build
from tcms.testcases.models import TestCase
from tcms.testplans.models import TestPlan
from tcms.testruns.models import TestRun, TestExecutionStatus, TestExecutionProperty
from . import roles
from .execution_identity import canonical_case_id
from .models import AIDefectDraft, AIRegressionVerification
from .regression_checks import failed_report_executions


def can_view_run(user, run):
    return user.has_perm("testruns.view_testrun") or user.has_perm("testruns.view_testrun", run)


def require_create(user):
    if roles.is_read_only(user) or not user.has_perms(
        ("testruns.add_testrun", "testruns.add_testexecution")
    ):
        raise PermissionDenied("没有新建复测执行任务的权限。")


class RegressionTaskForm(forms.Form):
    plan = forms.ModelChoiceField(queryset=TestPlan.objects.none(), label="测试计划")
    build = forms.ModelChoiceField(queryset=Build.objects.none(), label="修复构建")
    notes = forms.CharField(
        required=False, label="复测备注", widget=forms.Textarea(attrs={"rows": 3})
    )
    submission_token = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)

    def __init__(self, *args, user, source, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = get_objects_for_user(
            user, "testplans.change_testplan", klass=TestPlan
        ).filter(product_id=source.plan.product_id, is_active=True)
        self.fields["build"].queryset = Build.objects.filter(
            version__product_id=source.plan.product_id, is_active=True
        ).select_related("version")
        self.initial.setdefault("plan", source.plan_id)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean(self):
        data = super().clean()
        if (
            data.get("plan")
            and data.get("build")
            and data["plan"].product_version_id != data["build"].version_id
        ):
            raise forms.ValidationError("修复构建必须与所选测试计划版本一致。")
        return data


def source_executions(source, kind):
    return [source.execution] if kind == "defect" else failed_report_executions(source)


def create_task(user, source, kind, data):
    require_create(user)
    with transaction.atomic():
        # Serialise submissions per source. The token remains attached after
        # verification, so refreshing/resubmitting never creates a second task.
        source = type(source).objects.select_for_update().get(pk=source.pk)
        origin = source.execution.run if kind == "defect" else source.test_run
        if not can_view_run(user, origin):
            raise PermissionDenied
        token = str(data["submission_token"])
        relation = {"defect_draft": source} if kind == "defect" else {"source_report": source}
        existing = AIRegressionVerification.objects.filter(
            owner=user, result__submission_token=token, **relation
        ).first()
        if existing:
            if not can_view_run(user, existing.regression_run):
                raise PermissionDenied
            if (
                existing.regression_run.plan_id != data["plan"].pk
                or existing.regression_run.build_id != data["build"].pk
            ):
                raise ValueError("该表单已创建其他复测任务，请重新打开新建页面。")
            return existing.regression_run
        plan = get_object_or_404(
            get_objects_for_user(
                user, "testplans.change_testplan", klass=TestPlan
            ).select_for_update(),
            pk=data["plan"].pk,
            product_id=origin.plan.product_id,
            is_active=True,
        )
        build = get_object_or_404(
            Build, pk=data["build"].pk, version_id=plan.product_version_id, is_active=True
        )
        original = source_executions(source, kind)
        identities = Counter(canonical_case_id(execution) for execution in original)
        cases = {
            case.pk: case
            for case in TestCase.objects.select_for_update().filter(
                pk__in=identities, category__product_id=origin.plan.product_id
            )
        }
        if len(cases) != len(identities):
            raise ValueError("原失败用例已删除或移到其他项目，请先核对用例关联。")
        if not TestExecutionStatus.objects.filter(weight=0).exists():
            raise ValueError("请先配置未执行状态。")
        for case in cases.values():
            if not (
                user.has_perm("testcases.view_testcase")
                or user.has_perm("testcases.view_testcase", case)
            ):
                raise PermissionDenied
            if not case.case_status.is_confirmed:
                raise ValueError(f"TC-{case.pk} 尚未通过正式评审，请先确认用例再创建复测任务。")
        target = TestRun.objects.create(
            plan=plan,
            build=build,
            manager=user,
            default_tester=user,
            summary=f'缺陷复测 · {"缺陷" if kind == "defect" else "报告"} #{source.pk}',
            notes=f'来源任务 TR-{origin.pk}；{reverse("ai_assistant:edit_defect_draft" if kind == "defect" else "ai_assistant:edit_report", args=[source.pk])}\n{data.get("notes", "")}',
        )
        for permission in ("view_testrun", "change_testrun"):
            assign_perm("testruns." + permission, user, target)
        for index, execution in enumerate(original, start=1):
            case = cases[canonical_case_id(execution)]
            plan.add_case(case)
            copied = target._create_single_execution(case, user, build, index)
            for prop in execution.properties():
                TestExecutionProperty.objects.create(
                    execution=copied, name=prop.name, value=prop.value
                )
        result = {
            "task_created": True,
            "submission_token": token,
            "source_run_id": origin.pk,
            "regression_run_id": target.pk,
            "total_source_failures": len(original),
            "counts": {"passed": 0, "failed": 0, "pending": len(original), "missing": 0},
            "outcome": "pending",
            "items": [],
        }
        AIRegressionVerification.objects.create(
            owner=user,
            regression_run=target,
            status="incomplete",
            result=result,
            notes=data.get("notes", ""),
            **relation,
        )
        return target


def save_verification(**values):
    relation = {key: values[key] for key in ("source_report", "defect_draft") if key in values}
    with transaction.atomic():
        pending = (
            AIRegressionVerification.objects.select_for_update()
            .filter(
                owner=values["owner"],
                regression_run=values["regression_run"],
                result__task_created=True,
                **relation,
            )
            .first()
        )
        if pending:
            token = pending.result.get("submission_token")
            pending.status = values["status"]
            pending.result = dict(values["result"], submission_token=token)
            pending.notes = values.get("notes") or pending.notes
            pending.save(update_fields=("status", "result", "notes"))
            return pending
        return AIRegressionVerification.objects.create(**values)


@login_required
def new_task(request, kind, pk):
    require_create(request.user)
    if kind == "defect":
        source = get_object_or_404(
            AIDefectDraft.objects.select_related("execution__run"), pk=pk, owner=request.user
        )
        origin = source.execution.run
        back = reverse("ai_assistant:edit_defect_draft", args=[pk])
    elif kind == "report":
        source = get_object_or_404(
            roles.visible_reports(request.user).select_related("test_run"), pk=pk
        )
        origin = source.test_run
        back = reverse("ai_assistant:edit_report", args=[pk])
    else:
        raise PermissionDenied
    if not can_view_run(request.user, origin):
        raise PermissionDenied
    form = RegressionTaskForm(
        request.POST if request.method == "POST" else None, user=request.user, source=origin
    )
    if request.method == "POST" and form.is_valid():
        try:
            target = create_task(request.user, source, kind, form.cleaned_data)
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("testruns-get", pk=target.pk)
    return render(
        request,
        "ai_assistant/regression_task.html",
        {"form": form, "source": source, "origin": origin, "back_url": back},
    )
