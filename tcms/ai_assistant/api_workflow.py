"""Confirmed API suite submissions, with immutable formal-task bindings."""

import json
import uuid

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from guardian.shortcuts import assign_perm, get_objects_for_user

from tcms.management.models import Build
from tcms.testcases.models import TestCase
from tcms.testplans.models import TestPlan
from tcms.testplans.plan_library import numeric_id
from tcms.testruns.models import TestExecution, TestExecutionStatus, TestRun
from tcms.web_testing.workflow import FORMAL_PERMISSIONS

from .api_runner import submit_run
from .api_scheduling import suite_data
from .automation_ui import return_url, write_guard
from .case_library import visible_cases
from .crypto import decrypt_api_key, encrypt_api_key
from .models import APICase, APIEnvironment, APIRun, APISuite
from .roles import is_read_only


class SuiteSubmitForm(forms.Form):
    token = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    execution_mode = forms.ChoiceField(label="执行方式", choices=(
        ("formal", "正式执行"), ("debug", "调试执行")))
    plan = forms.ModelChoiceField(label="测试计划", queryset=TestPlan.objects.none(), required=False)
    build = forms.ModelChoiceField(label="测试构建", queryset=Build.objects.none(), required=False)
    environment = forms.ModelChoiceField(label="执行环境", queryset=APIEnvironment.objects.none(),
                                        required=False, empty_label="使用套件默认环境")

    def __init__(self, *args, owner, suite, **kwargs):
        self.owner, self.suite = owner, suite
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = get_objects_for_user(
            owner, "testplans.change_testplan", klass=TestPlan).filter(
                product=suite.product, product_version__product=suite.product, is_active=True)
        self.fields["build"].queryset = Build.objects.filter(version__product=suite.product, is_active=True)
        self.fields["environment"].queryset = APIEnvironment.objects.filter(owner=owner, product=suite.product)
        self.initial.setdefault("environment", suite.environment_id)
        if not self.is_bound:
            configs = APICase.objects.filter(owner=owner, product=suite.product, pk__in=suite.case_ids)
            can_formal = (owner.has_perms(FORMAL_PERMISSIONS) and not is_read_only(owner)
                          and self.fields["plan"].queryset.exists() and self.fields["build"].queryset.exists()
                          and bool(suite.case_ids) and configs.count() == len(suite.case_ids)
                          and not configs.filter(test_case__isnull=True).exists())
            self.initial.setdefault("execution_mode", "formal" if can_formal else "debug")
        for field in self.fields.values():
            if not field.widget.is_hidden:
                field.widget.attrs["class"] = "form-control"

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        if data["execution_mode"] != "formal":
            return data
        for key in ("plan", "build", "environment"):
            if not data.get(key):
                self.add_error(key, "正式执行必须选择此项。")
        if not self.owner.has_perms(FORMAL_PERMISSIONS) or is_read_only(self.owner):
            self.add_error(None, "没有创建正式执行任务及回写结果的权限，可以使用调试执行。")
        if data.get("plan") and data.get("build") and data["plan"].product_version_id != data["build"].version_id:
            self.add_error("build", "构建版本必须与测试计划版本一致。")
        if self.errors:
            return data
        configs = list(APICase.objects.filter(owner=self.owner, product=self.suite.product,
                                             pk__in=self.suite.case_ids))
        if len(configs) != len(self.suite.case_ids) or not configs:
            self.add_error(None, "套件脚本已变化，请重新编辑套件。")
            return data
        if any(not config.test_case_id for config in configs):
            self.add_error(None, "请先将全部接口脚本关联到用例库中的业务用例，再正式执行。")
            return data
        ids = {config.test_case_id for config in configs}
        accessible = set(visible_cases(self.owner).filter(pk__in=ids, category__product=self.suite.product)
                         .values_list("pk", flat=True))
        if accessible != ids:
            self.add_error(None, "关联业务用例不可访问或已更换项目，请重新配置。")
        elif set(data["plan"].cases.filter(pk__in=ids).values_list("pk", flat=True)) != ids:
            self.add_error(None, "请先将关联业务用例加入所选测试计划。")
        if not TestExecutionStatus.objects.filter(weight=0).exists():
            self.add_error(None, "未配置待执行状态，请联系平台管理员。")
        return data


def submission_choices(data):
    formal = data.get("execution_mode") == "formal"
    return {"execution_mode": data.get("execution_mode"),
            "environment": str(data.get("environment") or ""),
            "plan": str(data.get("plan") or "") if formal else "",
            "build": str(data.get("build") or "") if formal else ""}


def create_run(owner, suite, form, choices):
    """Called under owner/suite/config locks; any validation error rolls back both tasks."""
    data = form.cleaned_data
    target = None
    if data["execution_mode"] == "formal":
        pending = TestExecutionStatus.objects.select_for_update().filter(weight=0).order_by("pk").first()
        if pending is None:
            raise ValueError("未配置待执行状态，请联系平台管理员。")
        target = TestRun.objects.create(plan=data["plan"], build=data["build"], manager=owner,
            default_tester=owner, summary=f"接口自动化 · {suite.name}",
            notes="由接口正式执行创建；结果确认后回写，未执行与请求异常不计通过。")
        for permission in ("view_testrun", "change_testrun"):
            assign_perm("testruns." + permission, owner, target)
    queued = suite_data(suite, data["token"])
    queued["environment"] = data.get("environment") or suite.environment
    run = submit_run(owner, suite.product, queued, suite=suite)
    snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
    env = queued["environment"]
    context = {"environment_name": env.name, "base_url": env.base_url, "timeout": env.timeout}
    if target:
        context.update(product_id=suite.product_id, plan_id=target.plan_id, plan_name=target.plan.name,
            build_id=target.build_id, build_name=target.build.name, version_id=target.build.version_id,
            version_name=target.build.version.value, test_run_id=target.pk)
        for position, row in enumerate(snapshot["cases"], 1):
            if not row.get("test_case_id") or not row.get("business_case_version"):
                raise ValueError("无法确认关联业务用例版本，请重新配置后执行。")
            execution = TestExecution.objects.create(run=target, case_id=row["test_case_id"],
                build=data["build"], status=pending, assignee=owner,
                case_text_version=row["business_case_version"], sortkey=position)
            row["execution_id"] = execution.pk
    snapshot.update(execution_mode=data["execution_mode"], execution_context=context,
                    submission_choices=choices, writeback_mode="confirmation")
    run.test_run = target
    run.snapshot_encrypted = encrypt_api_key(json.dumps(snapshot, ensure_ascii=False))
    run.save(update_fields=("test_run", "snapshot_encrypted"))
    suite.last_triggered, suite.last_error = timezone.now(), ""
    suite.save(update_fields=("last_triggered", "last_error"))
    return run


@login_required
@never_cache
@write_guard
def submit(request, pk):
    suite = get_object_or_404(APISuite.objects.select_related("product", "environment"),
                             pk=pk, owner=request.user)
    form = SuiteSubmitForm(request.POST if request.method == "POST" else None,
                           owner=request.user, suite=suite)
    if request.method == "POST":
        with transaction.atomic():
            owner = get_user_model().objects.select_for_update().get(pk=request.user.pk)
            if not owner.is_active or is_read_only(owner):
                raise PermissionDenied
            try:
                token = uuid.UUID(request.POST.get("token", ""))
            except ValueError:
                token = None
            previous = APIRun.objects.filter(owner=owner, submission_token=token).first() if token else None
            choices = submission_choices(request.POST)
            if previous:
                captured = json.loads(decrypt_api_key(previous.snapshot_encrypted))
                if previous.suite_id != suite.pk or captured.get("submission_choices") != choices:
                    form.add_error(None, "这份表单已提交过其他选择，请重新打开执行页面。")
                else:
                    return redirect("ai_assistant:api_report", pk=previous.pk)
            else:
                suite = get_object_or_404(APISuite.objects.select_for_update().select_related("product", "environment"),
                                         pk=pk, owner=owner)
                env_id = numeric_id(request.POST.get("environment")) or suite.environment_id
                list(APIEnvironment.objects.select_for_update().filter(pk=env_id, owner=owner))
                list(TestPlan.objects.select_for_update().filter(pk=numeric_id(request.POST.get("plan"))))
                list(Build.objects.select_for_update().filter(pk=numeric_id(request.POST.get("build"))))
                configs = list(APICase.objects.select_for_update().filter(owner=owner, pk__in=suite.case_ids))
                list(TestCase.objects.select_for_update().filter(pk__in=[c.test_case_id for c in configs if c.test_case_id]))
                form = SuiteSubmitForm(request.POST, owner=owner, suite=suite)
                if form.is_valid():
                    if suite.runs.filter(status__in=APIRun.ACTIVE_STATUSES).exists():
                        form.add_error(None, "该套件已有排队或执行中的任务，请等待完成。")
                    elif APIRun.objects.filter(owner=owner, status__in=APIRun.ACTIVE_STATUSES).count() >= 20:
                        form.add_error(None, "待执行任务过多，请等待当前任务结束。")
                    else:
                        try:
                            with transaction.atomic():
                                run = create_run(owner, suite, form, choices)
                        except (ValueError, RuntimeError, ObjectDoesNotExist) as exc:
                            form.add_error(None, str(exc) if isinstance(exc, ValueError) else "套件配置已变化，请重新检查。")
                        else:
                            return redirect("ai_assistant:api_report", pk=run.pk)
    selected = form.fields["environment"].queryset.filter(pk=numeric_id(form["environment"].value())).first()
    effective = selected or suite.environment
    by_id = {c.pk: c for c in APICase.objects.filter(owner=request.user, product=suite.product,
                                                    pk__in=suite.case_ids).select_related("test_case")}
    configs = [by_id[pk] for pk in suite.case_ids if pk in by_id]
    previews = {str(env.pk): {"base_url": env.base_url, "timeout": env.timeout}
                for env in form.fields["environment"].queryset}
    previews[""] = {"base_url": suite.environment.base_url, "timeout": suite.environment.timeout}
    from .automation_data import datasets
    try:
        dataset_count = len(datasets(json.loads(decrypt_api_key(suite.datasets_encrypted) or "[]")) or [{}])
    except (ValueError, TypeError, RuntimeError):
        dataset_count = None
    response = render(request, "ai_assistant/api/submit.html", {
        "suite": suite, "product": suite.product, "form": form, "configs": configs,
        "dataset_count": dataset_count, "execution_count": len(configs) * (dataset_count or 0),
        "unlinked": [c for c in configs if not c.test_case_id],
        "preview": {"base_url": effective.base_url, "timeout": effective.timeout},
        "plan_versions": {str(p.pk): p.product_version_id for p in form.fields["plan"].queryset},
        "build_versions": {str(b.pk): b.version_id for b in form.fields["build"].queryset},
        "environment_previews": previews,
        "back_url": return_url(request, reverse("ai_assistant:api_suite", args=[suite.pk]))})
    response["Cache-Control"] = "private, no-store"
    return response
