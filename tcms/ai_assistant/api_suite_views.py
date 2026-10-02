import hashlib
import hmac
import json
import uuid
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache

from tcms.management.models import Product
from .api_forms import SuiteForm
from .api_runner import CASE_FIELDS, prepare_case, required_variables
from .api_scheduling import queue_suite, rotate_token
from .api_validation import validate_destination
from .api_views import home_url
from .crypto import decrypt_api_key
from .models import APIRun, APISuite


@login_required
@never_cache
def suite_edit(request, product_id, pk=None):
    product = get_object_or_404(Product, pk=product_id)
    suite = get_object_or_404(APISuite, owner=request.user, product=product, pk=pk) if pk else APISuite(owner=request.user, product=product)
    form = SuiteForm(request.POST if request.method == "POST" else None,
                     instance=suite, owner=request.user, product=product)
    if request.method == "POST" and form.is_valid():
        try:
            suite = form.save(commit=False)
            suite.case_ids = [case.pk for case in form.cleaned_data["cases"]]
            validate_destination(suite.environment.base_url)
            env = dict(headers=suite.environment.headers, variables=dict(suite.environment.variables),
                       secret_headers=json.loads(decrypt_api_key(suite.environment.secret_headers_encrypted) or "{}"))
            from .api_dataset_support import validate_dataset_cases
            validate_dataset_cases(list(form.cleaned_data["cases"]), env, form.cleaned_data.get("datasets"))
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=request.user.pk)
                # Only form-owned fields: never overwrite a concurrent token rotation.
                current = APISuite.objects.select_for_update().get(pk=suite.pk) if suite.pk else None
                if not suite.schedule_enabled:
                    suite.next_run_at = None
                elif (current and current.schedule_enabled and current.next_run_at
                      and current.interval_minutes == suite.interval_minutes):
                    suite.next_run_at = current.next_run_at
                else:
                    suite.next_run_at = timezone.now() + timedelta(minutes=suite.interval_minutes)
                if suite.pk:
                    suite.save(update_fields=("name", "environment", "case_ids", "stop_on_failure", "share_cookies",
                        "schedule_enabled", "interval_minutes", "next_run_at", "updated", "datasets_encrypted"))
                else:
                    suite.save()
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("ai_assistant:api_suite", pk=suite.pk)
    return render(request, "ai_assistant/api/form.html", dict(form=form, product=product,
        back_url=home_url(product), title="自动化套件与定时设置"))


def suite_context(suite):
    return dict(suite=suite, product=suite.product, back_url=home_url(suite.product),
                runs=suite.runs.all()[:20], timezone_name=timezone.get_current_timezone_name(),
                submission_token=uuid.uuid4())


@login_required
def suite_detail(request, pk):
    suite = get_object_or_404(APISuite, pk=pk, owner=request.user)
    return render(request, "ai_assistant/api/suite.html", suite_context(suite))


@login_required
@require_POST
def suite_action(request, pk, action):
    suite = get_object_or_404(APISuite, pk=pk, owner=request.user)
    if action == "execute":
        try:
            key = str(uuid.UUID(request.POST.get("submission_token", "")))
            run = queue_suite(suite.pk, request.user.pk, key=key)
        except (ValueError, ObjectDoesNotExist) as exc:
            messages.error(request, str(exc) if isinstance(exc, ValueError) else "套件配置已变化，请重新保存。")
        else:
            return redirect("ai_assistant:api_report", pk=run.pk)
    elif action in ("rotate-token", "revoke-token", "pause"):
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=request.user.pk)
            suite = APISuite.objects.select_for_update().get(pk=pk, owner=request.user)
            if action == "rotate-token":
                token = rotate_token(suite)
                response = render(request, "ai_assistant/api/suite.html", suite_context(suite) | {"new_token": token})
                response["Cache-Control"] = "no-store"
                response["Referrer-Policy"] = "no-referrer"
                return response
            if action == "revoke-token":
                suite.ci_token_hash, suite.ci_token_expires = "", None
                suite.save(update_fields=("ci_token_hash", "ci_token_expires"))
            else:
                suite.schedule_enabled, suite.next_run_at = False, None
                suite.save(update_fields=("schedule_enabled", "next_run_at"))
    else:
        return HttpResponse(status=404)
    return redirect("ai_assistant:api_suite", pk=pk)


def authenticate_ci(request, suite):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or len(header) > 256:
        return False
    digest = hashlib.sha256(header[7:].encode()).hexdigest()
    return (suite.owner.is_active and suite.ci_token_hash and suite.ci_token_expires
            and suite.ci_token_expires > timezone.now()
            and hmac.compare_digest(digest, suite.ci_token_hash))


def run_payload(run):
    results = list(run.results.values("position", "name", "status", "status_code", "elapsed_ms",
        "checks", "request_summary", "response_summary", "error", "writeback"))
    return dict(run_id=str(run.pk), status=run.status, terminal=run.is_terminal,
        passed=run.status == "completed" and bool(results) and all(r["status"] == "passed" for r in results),
        report_url=reverse("ai_assistant:api_report", args=[run.pk]), error=run.error, results=results)


@csrf_exempt
def ci_runs(request, pk, run_id=None):
    """Bearer-only endpoint: browser session cookies never authorize CI calls."""
    if request.method != ("GET" if run_id else "POST"):
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    suite = get_object_or_404(APISuite.objects.select_related("owner"), pk=pk)
    with transaction.atomic():
        # Recheck under the rotation lock to make revocation effective before submission.
        get_user_model().objects.select_for_update().get(pk=suite.owner_id)
        suite = APISuite.objects.select_for_update().select_related("owner").get(pk=pk)
        if not authenticate_ci(request, suite):
            response = JsonResponse({"error": "invalid_or_expired_token"}, status=401)
            response["WWW-Authenticate"] = "Bearer"
            return response
        if run_id:
            run = get_object_or_404(APIRun, pk=run_id, suite=suite, owner=suite.owner, trigger="ci")
        else:
            try:
                key = str(uuid.UUID(request.headers.get("Idempotency-Key", "")))
            except ValueError:
                return JsonResponse({"error": "Idempotency-Key 必须是 UUID；同一次构建重试使用同一值。"}, status=400)
            try:
                run = queue_suite(suite.pk, suite.owner_id, trigger="ci", key=key)
            except (ValueError, ObjectDoesNotExist):
                return JsonResponse({"error": "套件已有活动任务或配置无效，请在平台检查。"}, status=409)
    response = JsonResponse(run_payload(run), status=200 if run_id else 202)
    response["Cache-Control"] = "no-store"
    return response
