"""Explicit private debug executions; never publish or rewrite source runs."""
import json
import uuid
from types import SimpleNamespace

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache

from tcms.ai_assistant.crypto import decrypt_api_key, encrypt_api_key
from tcms.ai_assistant.roles import is_read_only
from .execution_config import suite_snapshot
from .models import WebCase, WebEnvironment, WebRun
from .views import write_guard


class DebugForm(forms.Form):
    token = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    environment = forms.ModelChoiceField(label="调试环境", queryset=WebEnvironment.objects.none())
    confirm = forms.BooleanField(label="确认在测试环境执行，调试结果不进入正式报告")

    def __init__(self, *args, owner, config, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["environment"].queryset = WebEnvironment.objects.filter(owner=owner, product_id=config.product_id)
        self.fields["environment"].widget.attrs["class"] = "form-control"


def locked_owner(user):
    owner = get_user_model().objects.select_for_update().get(pk=user.pk)
    if not owner.is_active or is_read_only(owner):
        raise PermissionDenied
    return owner


def check_quota(owner):
    if WebRun.objects.filter(owner=owner, status__in=["queued", "running"]).count() >= 20:
        raise ValueError("待执行任务过多，请等待当前任务结束。")


def debug_snapshot(config, environment):
    temporary = SimpleNamespace(owner=config.owner, product=config.product, product_id=config.product_id,
        base_url=environment.base_url, ignore_https_errors=environment.ignore_https_errors,
        environment_id=environment.pk, datasets_encrypted="", stop_on_failure=False, case_ids=[config.pk])
    snapshot = suite_snapshot(temporary, environment=environment)
    snapshot["debug_case_id"] = config.pk
    snapshot["execution_context"] = dict(environment_name=environment.name, base_url=environment.base_url,
        ignore_https_errors=environment.ignore_https_errors)
    return snapshot


@login_required
@write_guard
@never_cache
def debug_case(request, pk):
    if not request.user.is_active or is_read_only(request.user):
        raise PermissionDenied
    config = get_object_or_404(WebCase.objects.select_related("product"), pk=pk, owner=request.user)
    form = DebugForm(request.POST if request.method == "POST" else None, owner=request.user, config=config)
    if request.method == "POST":
        with transaction.atomic():
            owner = locked_owner(request.user)
            config = get_object_or_404(WebCase.objects.select_for_update().select_related("product"), pk=pk, owner=owner)
            form = DebugForm(request.POST, owner=owner, config=config)
            if form.is_valid():
                previous = WebRun.objects.filter(owner=owner, submission_token=form.cleaned_data["token"]).first()
                if previous:
                    snap = json.loads(decrypt_api_key(previous.snapshot_encrypted))
                    if previous.source_id or previous.execution_mode != "debug" or previous.environment_id != form.cleaned_data["environment"].pk or snap.get("debug_case_id") != config.pk:
                        raise PermissionDenied
                    return redirect("web_testing:run", pk=previous.pk)
                environment = get_object_or_404(WebEnvironment.objects.select_for_update(),
                    pk=form.cleaned_data["environment"].pk, owner=owner, product_id=config.product_id)
                try:
                    check_quota(owner)
                    snapshot = debug_snapshot(config, environment)
                except ValueError as exc:
                    form.add_error(None, str(exc))
                else:
                    run = WebRun.objects.create(owner=owner, product=config.product, suite=None,
                        name=("调试 · " + config.name)[:200], submission_token=form.cleaned_data["token"],
                        execution_mode="debug", environment=environment, total=1,
                        snapshot_encrypted=encrypt_api_key(json.dumps(snapshot, ensure_ascii=False)))
                    return redirect("web_testing:run", pk=run.pk)
    previews = {str(env.pk): dict(base_url=env.base_url, ignore_https_errors=env.ignore_https_errors)
                for env in form.fields["environment"].queryset}
    response = render(request, "web_testing/debug.html", dict(title="调试脚本", config=config,
        product=config.product, form=form, environment_previews=previews,
        environment_url=reverse("web_testing:environment_new") + "?product=" + str(config.product_id)))
    response["Cache-Control"] = "private, no-store"
    return response


def retry_snapshot(source, scope):
    snapshot = json.loads(decrypt_api_key(source.snapshot_encrypted))
    if scope == "all":
        return snapshot
    if scope != "failed":
        raise ValueError("无效的重跑范围。")
    selected = set(source.results.filter(status="failed").values_list("position", flat=True))
    cases = snapshot.get("cases", [])
    positions = [number for number in range(1, len(cases) + 1) if number in selected]
    if not positions:
        raise ValueError("本次执行没有可重跑的失败项。未执行项不属于失败项。")
    snapshot["cases"] = [dict(cases[number - 1], origin_position=cases[number - 1].get("origin_position", number))
                         for number in positions]
    # Every selected row retains its expanded dataset values and login/setup steps.
    snapshot["stop_on_failure"] = False
    snapshot["retry_context"] = dict(scope="failed", source_id=str(source.pk), source_positions=positions)
    return snapshot


def retry(request, pk):
    try:
        token = uuid.UUID(request.POST.get("token", ""))
    except (ValueError, TypeError):
        return HttpResponse("无效的提交标识。", status=400)
    scope = request.POST.get("scope", "all")
    if scope not in {"all", "failed"}:
        return HttpResponse("无效的重跑范围。", status=400)
    with transaction.atomic():
        owner = locked_owner(request.user)
        source = get_object_or_404(WebRun.objects.select_for_update(), pk=pk, owner=owner)
        if not source.terminal:
            return HttpResponse("请等待原任务结束。", status=409)
        try:
            snapshot = retry_snapshot(source, scope)
        except ValueError as exc:
            return HttpResponse(str(exc), status=409)
        previous = WebRun.objects.filter(owner=owner, submission_token=token).first()
        if previous:
            if previous.source_id != source.pk or previous.execution_mode != "debug" or json.loads(decrypt_api_key(previous.snapshot_encrypted)) != snapshot:
                raise PermissionDenied
            return redirect("web_testing:run", pk=previous.pk)
        try:
            check_quota(owner)
        except ValueError as exc:
            return HttpResponse(str(exc), status=409)
        run = WebRun.objects.create(owner=owner, product=source.product, suite=source.suite, source=source,
            name=("失败项重跑 · " + source.name)[:200] if scope == "failed" else source.name,
            submission_token=token, execution_mode="debug", environment=source.environment,
            total=len(snapshot["cases"]),
            snapshot_encrypted=source.snapshot_encrypted if scope == "all" else encrypt_api_key(json.dumps(snapshot, ensure_ascii=False)))
    return redirect("web_testing:run", pk=run.pk)
