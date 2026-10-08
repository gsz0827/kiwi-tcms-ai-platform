"""Explicit formal submissions and private, non-publishing debug runs."""
import json
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date
from guardian.shortcuts import assign_perm, get_objects_for_user

from tcms.ai_assistant.case_library import visible_cases
from tcms.ai_assistant.crypto import decrypt_api_key, encrypt_api_key
from tcms.ai_assistant.roles import is_read_only
from tcms.ai_assistant.automation_ui import return_url
from tcms.management.models import Build, Product, Version
from tcms.testplans.models import TestPlan
from tcms.testplans.plan_library import display_zone, local_display, numeric_id
from tcms.testruns.models import TestRun, TestExecution, TestExecutionStatus
from .execution_config import suite_snapshot
from .models import WebCase, WebEnvironment, WebRun, WebSuite


FORMAL_PERMISSIONS = ('testruns.add_testrun', 'testruns.add_testexecution', 'testruns.change_testexecution')


def initial_product(request):
    value = request.GET.get('product', request.session.get('ai_product_id', ''))
    if not value:
        return None
    return get_object_or_404(Product, pk=numeric_id(value))


def list_context(request, model, key):
    product = initial_product(request)
    data = request.GET.copy()
    data['product'] = str(product.pk) if product else ''
    items = model.objects.filter(owner=request.user).select_related('product')
    if product:
        items = items.filter(product=product)
    q = data.get('q', '').strip()[:200]
    if q:
        lookup = Q(name__icontains=q)
        if model is WebRun:
            try:
                lookup |= Q(pk=uuid.UUID(q))
            except ValueError:
                pass
        elif q.isascii() and q.isdecimal() and len(q) <= 18:
            lookup |= Q(pk=int(q))
        items = items.filter(lookup)
    errors = []
    versions = Version.objects.filter(product=product) if product else Version.objects.all()
    versions = versions.select_related('product').order_by('value', 'pk')
    if model is WebRun:
        from tcms.ai_assistant.run_outcomes import annotate_outcomes
        items = annotate_outcomes(items)
        items = items.select_related('test_run__plan', 'test_run__build', 'environment')
        if 'version' not in data:
            value = request.session.get('ai_version_id', '')
            # A project selected explicitly must not inherit another project's version.
            data['version'] = str(value) if versions.filter(pk=numeric_id(value)).exists() else ''
        if data.get('version'):
            version = versions.filter(pk=numeric_id(data['version'])).first()
            items = items.filter(test_run__build__version=version) if version else items.none()
        status = data.get('status', '')
        if status:
            items = items.filter(status=status) if status in dict(WebRun.STATES) else items.none()
        zone = display_zone(request)
        for field, lookup in [('after', 'created__gte'), ('before', 'created__lt')]:
            if not data.get(field):
                continue
            try:
                date = parse_date(data[field])
            except ValueError:
                date = None
            if date is None or not 2 <= date.year <= 9998:
                errors.append('日期格式不正确，请重新选择。')
                items = items.none()
            else:
                if field == 'before':
                    date += timedelta(days=1)
                boundary = datetime.combine(date, time.min, tzinfo=zone)
                if not settings.USE_TZ:
                    boundary = boundary.astimezone(ZoneInfo(settings.TIME_ZONE)).replace(tzinfo=None)
                items = items.filter(**{lookup: boundary})
    elif model is WebSuite:
        items = items.select_related('environment')
    else:
        items = items.select_related('setup_case')
    page = Paginator(items.order_by('-created' if model is WebRun else '-updated', '-pk'), 30).get_page(data.get('page'))
    zone = display_zone(request)
    for item in page:
        item.display_time = local_display(item.created if model is WebRun else item.updated, zone)
    params = data.copy()
    params.pop('page', None)
    return {key: page, 'products': Product.objects.order_by('name', 'pk'), 'product': product,
            'filters': data, 'query': params.urlencode(), 'versions': versions,
            'run_states': WebRun.STATES, 'filter_errors': errors, 'can_write': not is_read_only(request.user)}


class SubmitForm(forms.Form):
    token = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    execution_mode = forms.ChoiceField(label='执行方式', initial='formal',
                                      choices=(('formal', '正式执行'), ('debug', '调试执行')))
    plan = forms.ModelChoiceField(label='测试计划', queryset=TestPlan.objects.none(), required=False)
    build = forms.ModelChoiceField(label='测试构建', queryset=Build.objects.none(), required=False)
    environment = forms.ModelChoiceField(label='执行环境', queryset=WebEnvironment.objects.none(), required=False,
                                        empty_label='使用套件默认环境')

    def __init__(self, *args, owner, suite, **kwargs):
        self.owner, self.suite = owner, suite
        super().__init__(*args, **kwargs)
        self.fields['plan'].queryset = get_objects_for_user(owner, 'testplans.change_testplan', klass=TestPlan).filter(
            product=suite.product, product_version__product=suite.product, is_active=True).select_related('product_version')
        self.fields['build'].queryset = Build.objects.filter(version__product=suite.product, is_active=True).select_related('version')
        self.fields['environment'].queryset = WebEnvironment.objects.filter(owner=owner, product=suite.product)
        self.initial.setdefault('environment', suite.environment_id)
        if not self.is_bound and 'execution_mode' not in self.initial:
            configs = WebCase.objects.filter(owner=owner, product=suite.product, pk__in=suite.case_ids)
            can_formal = (owner.has_perms(FORMAL_PERMISSIONS) and not is_read_only(owner)
                          and self.fields['plan'].queryset.exists()
                          and self.fields['build'].queryset.exists()
                          and suite.environment_id is not None
                          and bool(suite.case_ids) and configs.count() == len(suite.case_ids)
                          and not configs.filter(test_case__isnull=True).exists())
            self.initial['execution_mode'] = 'formal' if can_formal else 'debug'
        for field in self.fields.values():
            if not field.widget.is_hidden:
                field.widget.attrs['class'] = 'form-control'

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        formal = data.get('execution_mode') == 'formal'
        if formal:
            for key in ('plan', 'build', 'environment'):
                if not data.get(key):
                    self.add_error(key, '正式执行必须选择此项。')
            if not self.owner.has_perms(FORMAL_PERMISSIONS) or is_read_only(self.owner):
                self.add_error(None, '没有创建正式执行任务及回写结果的权限，可以使用调试执行。')
            if data.get('plan') and data.get('build') and data['plan'].product_version_id != data['build'].version_id:
                self.add_error('build', '构建版本必须与测试计划版本一致。')
        if self.errors:
            return data
        try:
            self.snapshot = suite_snapshot(self.suite, environment=data.get('environment'))
            if formal:
                rows = self.snapshot['cases']
                missing = sorted({row['id'] for row in rows if not row.get('business_case_id') or not row.get('business_case_version')})
                if missing:
                    raise ValueError('以下配置未关联有效业务用例，不能正式执行：' + '、'.join(f'WEB-{pk}' for pk in missing))
                ids = {row['business_case_id'] for row in rows}
                visible = set(visible_cases(self.owner).filter(pk__in=ids, category__product=self.suite.product).values_list('pk', flat=True))
                if visible != ids:
                    raise ValueError('部分关联业务用例不可访问或已更换项目，请重新配置。')
                included = set(data['plan'].cases.filter(pk__in=ids).values_list('pk', flat=True))
                if included != ids:
                    raise ValueError('请先将这些业务用例加入所选测试计划：' + '、'.join(f'TC-{pk}' for pk in sorted(ids - included)))
                if not TestExecutionStatus.objects.filter(weight=0).exists():
                    raise ValueError('未配置待执行状态，请联系平台管理员。')
        except (ValueError, RuntimeError) as exc:
            self.add_error(None, str(exc))
        return data


def execution_context(suite, data, target=None):
    env = data.get('environment') or suite.environment
    context = {'environment_name': env.name if env else '套件站点',
               'base_url': env.base_url if env else suite.base_url,
               'ignore_https_errors': env.ignore_https_errors if env else suite.ignore_https_errors}
    if target:
        context.update(product_id=suite.product_id, plan_id=target.plan_id, plan_name=target.plan.name,
                       build_id=target.build_id, build_name=target.build.name,
                       version_id=target.build.version_id, version_name=target.build.version.value,
                       test_run_id=target.pk)
    return context


def create_run(owner, suite, form):
    data, snapshot = form.cleaned_data, form.snapshot
    target = None
    if data['execution_mode'] == 'formal':
        target = TestRun.objects.create(plan=data['plan'], build=data['build'], manager=owner, default_tester=owner,
                                       summary=f'Web 自动化 · {suite.name}', notes='由 Web 正式执行创建；后台结果需确认后回写，未执行项不计通过。')
        for permission in ('view_testrun', 'change_testrun'):
            assign_perm('testruns.' + permission, owner, target)
        pending = TestExecutionStatus.objects.filter(weight=0).order_by('pk').first()
        for position, row in enumerate(snapshot['cases'], 1):
            TestExecution.objects.create(run=target, case_id=row['business_case_id'], build=data['build'], status=pending,
                                         assignee=owner, case_text_version=row['business_case_version'], sortkey=position)
    snapshot['execution_context'] = execution_context(suite, data, target)
    return WebRun.objects.create(owner=owner, product=suite.product, suite=suite, name=suite.name,
                                 submission_token=data['token'], execution_mode=data['execution_mode'], test_run=target,
                                 environment=data.get('environment') or suite.environment,
                                 snapshot_encrypted=encrypt_api_key(json.dumps(snapshot, ensure_ascii=False)), total=len(snapshot['cases']))


def submit(request, pk):
    suite = get_object_or_404(WebSuite.objects.select_related('product', 'environment'), pk=pk, owner=request.user)
    initial = {}
    if request.GET.get('plan'):
        initial['plan'] = numeric_id(request.GET['plan'])
    form = SubmitForm(request.POST if request.method == 'POST' else None, owner=request.user, suite=suite, initial=initial)
    if request.method == 'POST':
        # Account lock makes quota, idempotency and paired native-task creation atomic.
        with transaction.atomic():
            owner = get_user_model().objects.select_for_update().get(pk=request.user.pk)
            if not owner.is_active or is_read_only(owner):
                raise PermissionDenied
            token = None
            try:
                token = uuid.UUID(request.POST.get('token', ''))
            except ValueError:
                pass
            previous = WebRun.objects.filter(owner=owner, submission_token=token).first() if token else None
            if previous:
                if previous.suite_id != suite.pk:
                    raise PermissionDenied('提交标识已用于其他套件。')
                return redirect('web_testing:run', pk=previous.pk)
            suite = get_object_or_404(WebSuite.objects.select_for_update().select_related('product', 'environment'), pk=pk, owner=owner)
            # Serialize changes to selected execution environment and plan while capturing.
            env_id, plan_id = numeric_id(request.POST.get('environment')), numeric_id(request.POST.get('plan'))
            if env_id:
                list(WebEnvironment.objects.select_for_update().filter(pk=env_id, owner=owner))
            if plan_id:
                list(TestPlan.objects.select_for_update().filter(pk=plan_id))
            list(WebCase.objects.select_for_update().filter(pk__in=suite.case_ids, owner=owner, product=suite.product))
            from tcms.testcases.models import TestCase
            list(TestCase.objects.select_for_update().filter(web_configs__pk__in=suite.case_ids).distinct())
            form = SubmitForm(request.POST, owner=owner, suite=suite)
            if form.is_valid():
                if WebRun.objects.filter(owner=owner, status__in=('queued', 'running')).count() >= 20:
                    form.add_error(None, '待执行任务过多，请等待当前任务结束。')
                else:
                    run = create_run(owner, suite, form)
                    return redirect('web_testing:run', pk=run.pk)
    selected_env = form.fields['environment'].queryset.filter(pk=numeric_id(form['environment'].value())).first()
    preview = execution_context(suite, {'environment': selected_env})
    configs = WebCase.objects.filter(owner=request.user, product=suite.product, pk__in=suite.case_ids).select_related('test_case')
    unlinked = [config for config in configs if not config.test_case_id]
    plan_versions = {str(plan.pk): plan.product_version_id for plan in form.fields['plan'].queryset}
    build_versions = {str(build.pk): build.version_id for build in form.fields['build'].queryset}
    environment_previews = {str(env.pk): {'base_url': env.base_url, 'ignore_https_errors': env.ignore_https_errors}
                            for env in form.fields['environment'].queryset}
    environment_previews[''] = execution_context(suite, {})
    response = render(request, 'web_testing/submit.html', {'suite': suite, 'form': form, 'preview': preview,
                      'unlinked': unlinked, 'configs': configs, 'plan_versions': plan_versions, 'build_versions': build_versions,
                      'environment_previews': environment_previews,
                      'title': '执行测试套件', 'back_url':return_url(request, reverse('web_testing:suites')+'?product='+str(suite.product_id))})
    response['Cache-Control'] = 'private, no-store'
    return response


def run_context(run):
    from .regression import run_context as regression_context
    snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
    context = snapshot.get('execution_context', {})
    rows = snapshot.get('cases', [])
    return {**regression_context(run), 'frozen_context': context, 'frozen_base_url': snapshot.get('base_url', ''), 'retry_context': snapshot.get('retry_context', {}),
            'can_publish': bool(rows) and run.execution_mode != 'debug' and all(
                row.get('business_case_id') and (run.execution_mode != 'formal' or row.get('business_case_version')) for row in rows)}


def validate_archive_binding(source, data):
    if source.execution_mode == 'debug':
        raise ValueError('调试执行不进入正式报告，请选择正式执行并关联测试计划。')
    if source.execution_mode != 'formal':
        return data
    target = source.test_run
    frozen = json.loads(decrypt_api_key(source.snapshot_encrypted)).get('execution_context', {})
    if not target or (target.pk, target.plan_id, target.build_id) != (
            frozen.get('test_run_id'), frozen.get('plan_id'), frozen.get('build_id')):
        raise ValueError('正式执行的计划或构建关联已变更，不能回写，请重新执行。')
    if (target.plan.product_id != source.product_id or target.build.version.product_id != source.product_id
            or target.plan.product_version_id != frozen.get('version_id')
            or target.build.version_id != frozen.get('version_id')):
        raise ValueError('正式执行的项目或发布版本已变更，不能回写，请重新执行。')
    if getattr(data.get('plan'), 'pk', None) != target.plan_id or getattr(data.get('build'), 'pk', None) != target.build_id:
        raise ValueError('正式执行必须归档到提交时选定的计划和构建。')
    if data.get('target_run') and data['target_run'].pk != target.pk:
        raise ValueError('正式执行不能回写到其他任务。')
    return dict(data, target_run=target)
