"""Suite-scoped, bearer-only Web CI; reuse the browser's formal validation."""
import json
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from tcms.ai_assistant.api_suite_views import authenticate_ci
from tcms.ai_assistant.api_scheduling import rotate_token
from tcms.ai_assistant.roles import is_read_only
from .models import WebSuite, WebRun, WebCase, WebEnvironment
from .workflow import SubmitForm, create_run
from .views import write_guard


def context(suite):
    return dict(suite=suite, title='Jenkins / CI 接入',
                back_url=reverse('web_testing:suite_edit', args=[suite.pk]))


@login_required
@never_cache
def settings(request, pk):
    suite = get_object_or_404(WebSuite, pk=pk, owner=request.user)
    return render(request, 'web_testing/ci.html', context(suite))


@login_required
@require_POST
@never_cache
@write_guard
def token_action(request, pk, action):
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        suite = get_object_or_404(WebSuite.objects.select_for_update(), pk=pk, owner=request.user)
        if action == 'rotate-token':
            response = render(request, 'web_testing/ci.html', context(suite) | {'new_token': rotate_token(suite)})
            response['Referrer-Policy'] = 'no-referrer'
            return response
        if action != 'revoke-token':
            return HttpResponse(status=404)
        suite.ci_token_hash, suite.ci_token_expires = '', None
        suite.save(update_fields=('ci_token_hash', 'ci_token_expires'))
    return redirect('web_testing:ci_settings', pk=pk)


def run_payload(run):
    results = list(run.results.values('position', 'name', 'status', 'elapsed_ms', 'error', 'steps'))
    return dict(run_id=str(run.pk), status=run.status, terminal=run.terminal,
                passed=run.status == 'passed' and len(results) == run.total and bool(results)
                and all(row['status'] == 'passed' for row in results),
                report_url=reverse('web_testing:run', args=[run.pk]),
                execution_mode=run.execution_mode, test_run_id=run.test_run_id,
                error=run.error, results=results)


@csrf_exempt
@never_cache
def runs(request, pk, run_id=None):
    if request.method != ('GET' if run_id else 'POST'):
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    suite = get_object_or_404(WebSuite.objects.select_related('owner'), pk=pk)
    with transaction.atomic():
        owner = get_user_model().objects.select_for_update().get(pk=suite.owner_id)
        suite = WebSuite.objects.select_for_update().select_related('owner', 'product', 'environment').get(pk=pk)
        if not authenticate_ci(request, suite):
            response = JsonResponse({'error': 'invalid_or_expired_token'}, status=401)
            response['WWW-Authenticate'] = 'Bearer'
            return response
        if is_read_only(owner):
            return JsonResponse({'error': 'readonly_account'}, status=403)
        if run_id:
            run = get_object_or_404(WebRun, pk=run_id, suite=suite, owner=owner, trigger='ci')
        else:
            try:
                key = uuid.UUID(request.headers.get('Idempotency-Key', ''))
                if len(request.body) > 4096:
                    raise ValueError
                data = json.loads(request.body or b'{}')
                if not isinstance(data, dict) or set(data) - {'execution_mode', 'plan', 'build', 'environment'}:
                    raise ValueError
            except (ValueError, UnicodeError, RecursionError):
                return JsonResponse({'error': '需有效 UUID 提交标识及 JSON 执行配置。'}, status=400)
            previous = WebRun.objects.filter(owner=owner, submission_token=key).first()
            if previous:
                if previous.suite_id != suite.pk or previous.trigger != 'ci':
                    return JsonResponse({'error': 'submission_key_conflict'}, status=409)
                run = previous
            else:
                if suite.webrun_set.filter(status__in=('queued', 'running')).exists() or WebRun.objects.filter(
                        owner=owner, status__in=('queued', 'running')).count() >= 20:
                    return JsonResponse({'error': '套件已有活动执行，或待执行任务过多。'}, status=409)
                data = dict(data, token=key)
                data.setdefault('execution_mode', 'debug')
                data.setdefault('environment', suite.environment_id or '')
                from tcms.testplans.models import TestPlan
                from tcms.testcases.models import TestCase
                from .workflow import numeric_id
                if data.get('environment'):
                    list(WebEnvironment.objects.select_for_update().filter(pk=numeric_id(str(data['environment'])), owner=owner))
                if data.get('plan'):
                    list(TestPlan.objects.select_for_update().filter(pk=numeric_id(str(data['plan']))))
                list(WebCase.objects.select_for_update().filter(pk__in=suite.case_ids, owner=owner, product=suite.product))
                list(TestCase.objects.select_for_update().filter(web_configs__pk__in=suite.case_ids).distinct())
                form = SubmitForm(data, owner=owner, suite=suite)
                try:
                    if not form.is_valid():
                        return JsonResponse({'error': '执行配置无效，请核对计划、构建、环境及业务用例。'}, status=409)
                    run = create_run(owner, suite, form)
                    run.trigger = 'ci'
                    run.save(update_fields=('trigger',))
                except (ValueError, ObjectDoesNotExist):
                    return JsonResponse({'error': '套件配置已变化，请在平台重新检查。'}, status=409)
    return JsonResponse(run_payload(run), status=200 if run_id else 202)
