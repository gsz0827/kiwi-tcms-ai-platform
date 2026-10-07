"""Explicit, idempotent publication of private execution summaries to TCMS."""
import json
from django import forms
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from guardian.shortcuts import assign_perm, get_objects_for_user
from tcms.management.models import Build
from .execution_identity import resolve_archive_case
from tcms.testplans.models import TestPlan
from tcms.testruns.models import TestRun, TestExecution, TestExecutionStatus
from .crypto import decrypt_api_key
from .models import APIRun, AutomationArchive, AITestReport, AIDefectDraft
from .roles import is_read_only


def source_run(user, kind, pk, lock=False):
    from tcms.web_testing.models import WebRun
    if kind not in {'web','api'}: raise PermissionDenied
    query = (WebRun if kind=='web' else APIRun).objects
    if lock: query = query.select_for_update()
    return get_object_or_404(query, owner=user, pk=pk)


def source_rows(run, kind):
    snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
    actual = {r.position:r for r in run.results.all().defer('screenshot')} if kind=='web' else {r.position:r for r in run.results.all()}
    rows = []
    for index, case in enumerate(snapshot['cases'],1 if kind=='web' else 0):
        result = actual.get(index)
        rows.append({'position':index, 'name':case['name'], 'status':result.status if result else 'pending',
            'elapsed_ms':(result.elapsed_ms or 0) if result else 0,
            'business_case_id':case.get('business_case_id' if kind=='web' else 'test_case_id'),
            'business_case_version':case.get('business_case_version'),
            'automation_case_id':case.get('id' if kind=='web' else 'case_id'),
            'dataset':case.get('dataset',0)})
    return rows


def require_archive_permission(user):
    if is_read_only(user) or not user.has_perms(('testruns.add_testrun','testruns.add_testexecution')):
        raise PermissionDenied('需要创建执行任务和执行记录的权限才能归档。')


class ArchiveForm(forms.Form):
    plan = forms.ModelChoiceField(queryset=TestPlan.objects.none(),label='归属测试计划')
    build = forms.ModelChoiceField(queryset=Build.objects.none(),label='测试构建版本')
    passed = forms.ModelChoiceField(queryset=TestExecutionStatus.objects.filter(weight__gt=0),label='通过状态')
    failed = forms.ModelChoiceField(queryset=TestExecutionStatus.objects.filter(weight__lt=0),label='失败状态')
    pending = forms.ModelChoiceField(queryset=TestExecutionStatus.objects.filter(weight=0),label='未执行/环境异常状态')
    defects = forms.MultipleChoiceField(required=False,widget=forms.CheckboxSelectMultiple,label='确认创建缺陷草稿的失败项')
    target_run = forms.ModelChoiceField(queryset=TestRun.objects.none(),required=False,label='回写到执行任务（可选）',help_text='选择本次自动化提交前创建的未执行任务；不选则单独归档。')
    confirm = forms.BooleanField(label='确认将用例名称、状态、耗时和来源链接纳入项目报告（不包含请求参数、登录信息或截图）')

    def __init__(self,*args,owner,product,rows,source=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['plan'].queryset = get_objects_for_user(owner,'testplans.change_testplan',klass=TestPlan).filter(product=product,is_active=True)
        self.fields['build'].queryset = Build.objects.filter(version__product=product,is_active=True)
        self.fields['target_run'].queryset = get_objects_for_user(owner,'testruns.change_testrun',klass=TestRun).filter(plan__product=product,stop_date__isnull=True).exclude(automationarchive__isnull=False)
        self.fields['defects'].choices = [(str(r['position']),r['name']) for r in rows if r['status']=='failed']
        if source is not None and getattr(source, "execution_mode", "") == "formal" and source.test_run_id:
            target = source.test_run
            self.fields['target_run'].label = '回写到本次执行任务'
            self.fields['target_run'].help_text = '正式执行的计划、构建和执行任务已锁定，结果确认后回写。'
            for name, value in (("plan", target.plan_id), ("build", target.build_id), ("target_run", target.pk)):
                self.initial[name] = value
                self.fields[name].disabled = True
                self.fields[name].required = True
        for field in self.fields.values():
            if not isinstance(field.widget,(forms.CheckboxInput,forms.CheckboxSelectMultiple)): field.widget.attrs['class']='form-control'

    def clean(self):
        data=super().clean()
        if data.get('plan') and data.get('build') and data['plan'].product_version_id != data['build'].version_id:
            raise forms.ValidationError('构建版本必须与测试计划版本一致。')
        target = data.get('target_run')
        if target and (target.plan_id != getattr(data.get('plan'),'pk',None) or target.build_id != getattr(data.get('build'),'pk',None)):
            raise forms.ValidationError('回写任务必须属于所选计划和构建。')
        return data


def publish(user,kind,pk,data):
    from .services import build_test_run_snapshot
    from .engineering import canonical_hash, evaluate_release_gate, derived_release_decision, defect_fingerprint
    with transaction.atomic():
        source = source_run(user,kind,pk,lock=True)
        existing = AutomationArchive.objects.filter(kind=kind,source_id=pk,owner=user).first()
        if existing: return existing
        require_archive_permission(user)
        if not data.get('confirm'):
            raise ValueError('请先确认共享归档摘要。')
        if not (source.terminal if kind=='web' else source.is_terminal):
            raise ValueError('任务尚未结束，不能归档。')
        if kind == "web":
            from tcms.web_testing.workflow import validate_archive_binding
            data = validate_archive_binding(source, data)
        plan = get_object_or_404(get_objects_for_user(user,'testplans.change_testplan',klass=TestPlan).select_for_update(),pk=data['plan'].pk,product=source.product,is_active=True)
        build = get_object_or_404(Build,pk=data['build'].pk,version_id=plan.product_version_id,is_active=True)
        rows = source_rows(source,kind)
        chosen = set(data.get('defects',[]))
        if not chosen <= {str(r['position']) for r in rows if r['status']=='failed'}: raise PermissionDenied
        statuses = {}
        for key, compare in [('passed','gt'),('failed','lt'),('pending','exact')]:
            statuses[key] = get_object_or_404(TestExecutionStatus,pk=data[key].pk,**{'weight__'+compare:0})
        identities = [resolve_archive_case(user,source,row) for row in rows]
        if not identities: raise ValueError('本次执行没有可归档的用例。')
        source_url = reverse('web_testing:run' if kind=='web' else 'ai_assistant:api_report',args=[source.pk])
        label = 'Web' if kind=='web' else '接口'
        from .archive_task_target import prepare_target
        target, grouped = prepare_target(user, source, data, identities)
        reuse_target = target is not None
        completed = (source.finished if kind=='web' else source.completed) or timezone.now()
        if target is None:
            target = TestRun.objects.create(plan=plan,build=build,manager=user,default_tester=user,
                summary=f'{label}自动化归档 · {source.pk}',notes=f'来源：{source_url}\n仅保存执行摘要；原始私有配置不公开。',
                start_date=source.started,stop_date=completed)
            for permission in ['view_testrun','change_testrun']: assign_perm('testruns.'+permission,user,target)
        mapping, defects = [], []
        for row, (case, case_version) in zip(rows, identities):
            state = row['status'] if row['status'] in {'passed','failed'} else 'pending'
            row['business_case_id'] = case.pk
            row['business_case_version'] = case_version
            plan.add_case(case)
            if reuse_target:
                execution = grouped[(case.pk,case_version)].pop(0)
                execution.status = statuses[state]
                execution.tested_by = user if state!='pending' else None
                execution.start_date = source.started
                execution.stop_date = completed if state!='pending' else None
                execution._history_user = user
                execution._change_reason = f'{label}自动化归档 {source.pk}'
                execution.save()
            else:
                execution = TestExecution.objects.create(run=target,case=case,build=build,status=statuses[state],assignee=user,tested_by=user if state!='pending' else None,
                    case_text_version=case_version,sortkey=row['position'],start_date=source.started,stop_date=completed if state!='pending' else None)
            mapping.append(dict(row,execution_id=execution.pk))
            if str(row['position']) in chosen:
                draft = AIDefectDraft.objects.create(owner=user,execution=execution,title=(row['name']+'：自动化失败待排查')[:255],
                    description='由自动化执行摘要创建，需人工确认根因和复现条件。',actual_result='自动化断言或前置步骤失败，详见原执行记录。',
                    expected_result='按用例预期通过。',evidence=[source_url],environment=f'{label}自动化；构建 {build.name}')
                draft.fingerprint=defect_fingerprint(draft); draft.save(update_fields=('fingerprint','updated'))
                defects.append({'id':draft.pk,'title':draft.title,'priority':draft.priority,'status':draft.status})
        if reuse_target:
            target.start_date = source.started
            target.stop_date = completed if all(row['status'] in {'passed','failed'} for row in rows) else None
            target.save(update_fields=('start_date','stop_date'))
        snapshot = build_test_run_snapshot(target)
        snapshot['automation_source']={'kind':kind,'id':str(source.pk),'results':rows}
        report = AITestReport.objects.create(owner=user,test_run=target,title=f'{label}自动化测试报告',
            summary=f'归档 {len(rows)} 条执行结果；未执行与环境异常不计为通过。',scope='已选自动化执行的不可变摘要快照',
            conclusion='请结合失败项和未执行项审批，不自动发布。',metrics_snapshot=snapshot,snapshot_hash=canonical_hash(snapshot),defect_summary=defects)
        report.gate_result=evaluate_release_gate(source.product,snapshot,[target.pk])
        report.release_decision=derived_release_decision(report.gate_result,report.approval_status)
        report.save(update_fields=('gate_result','release_decision','updated'))
        return AutomationArchive.objects.create(owner=user,kind=kind,source_id=source.pk,test_run=target,report=report,results=mapping)


@login_required
def archive(request,kind,pk):
    source=source_run(request.user,kind,pk)
    existing=AutomationArchive.objects.filter(owner=request.user,kind=kind,source_id=pk).first()
    if existing: return redirect('ai_assistant:edit_report',pk=existing.report_id)
    require_archive_permission(request.user)
    rows=source_rows(source,kind)
    if kind == "web" and source.execution_mode == "debug":
        return HttpResponse("调试执行不进入正式报告，请发起正式执行。", status=409)
    form=ArchiveForm(request.POST if request.method=='POST' else None,owner=request.user,product=source.product,rows=rows,source=source if kind == "web" else None)
    if request.method=='POST' and form.is_valid():
        try: saved=publish(request.user,kind,pk,form.cleaned_data)
        except ValueError as exc: form.add_error(None,str(exc))
        else:
            if kind == 'web' and source.execution_mode == 'formal' and json.loads(decrypt_api_key(source.snapshot_encrypted)).get('regression_context'):
                return redirect('web_testing:run', pk=source.pk)
            return redirect('ai_assistant:edit_report',pk=saved.report_id)
    template = 'web_testing/archive.html' if kind == 'web' else 'ai_assistant/api/form.html'
    return render(request,template,{'form':form,'source':source,'product':source.product,'title':'归档测试报告与缺陷草稿','back_url':reverse('web_testing:run' if kind=='web' else 'ai_assistant:api_report',args=[pk])})
