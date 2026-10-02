import json
import re
from django import forms
from tcms.ai_assistant.automation_data import variables, datasets
from tcms.ai_assistant.crypto import encrypt_api_key, decrypt_api_key
from .models import WebCase, WebEnvironment
from .validation import validate_url, validate_steps


def expand_steps(steps, values):
    def expand(text):
        def substitute(match):
            key = match.group(1)
            if key not in values:
                raise ValueError('缺少测试变量：'+key)
            return str(values[key])
        return re.sub(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}', substitute, text)
    expanded = [{key:expand(value) if key != 'action' else value for key,value in step.items()} for step in steps]
    return validate_steps(expanded)


class EnvironmentForm(forms.ModelForm):
    variables = forms.JSONField(required=False, label='环境变量', widget=forms.Textarea(attrs={'rows':4}),
        help_text='例如 {"username":"demo"}。步骤中使用 {{username}} 引用；仅本人可见，加密保存。')

    class Meta:
        model = WebEnvironment
        fields = ('product','name','base_url','variables','setup_case','ignore_https_errors')
        labels = {'product':'产品', 'setup_case':'公共登录/前置用例'}
        help_texts = {'setup_case':'可选。每组数据先执行一次，成功后将 Cookie 与本地存储复制给该组各用例；不会跨执行保存登录态。'}

    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)
        self.fields['setup_case'].queryset = WebCase.objects.filter(owner=owner)
        if self.instance.pk:
            self.initial['variables'] = json.loads(decrypt_api_key(self.instance.variables_encrypted) or '{}')
        for field in self.fields.values():
            if not isinstance(field.widget, forms.CheckboxInput): field.widget.attrs['class']='form-control'

    def clean(self):
        data = super().clean()
        try:
            variables(data.get('variables') or {})
            if data.get('base_url'): validate_url(data['base_url'])
            if data.get('setup_case') and data['setup_case'].product_id != getattr(data.get('product'),'pk',None):
                raise ValueError('公共登录用例必须属于所选产品。')
            if self.instance.pk and getattr(data.get('product'),'pk',None) != self.instance.product_id:
                raise ValueError('已有环境不能更换产品，请新建环境。')
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return data

    def save(self, commit=True):
        self.instance.owner = self.owner
        self.instance.variables_encrypted = encrypt_api_key(json.dumps(self.cleaned_data.get('variables') or {}))
        return super().save(commit)


def suite_snapshot(suite):
    base_url, ignore_https = suite.base_url, suite.ignore_https_errors
    values, setup = {}, []
    if suite.environment_id:
        env = WebEnvironment.objects.get(pk=suite.environment_id, owner=suite.owner, product=suite.product)
        base_url, ignore_https = env.base_url, env.ignore_https_errors
        values = variables(json.loads(decrypt_api_key(env.variables_encrypted) or '{}'))
        if env.setup_case_id:
            login = WebCase.objects.get(pk=env.setup_case_id, owner=suite.owner, product=suite.product)
            setup = json.loads(decrypt_api_key(login.steps_encrypted))
    validate_url(base_url)
    rows = datasets(json.loads(decrypt_api_key(suite.datasets_encrypted) or '[]')) or [{}]
    selected = {c.pk:c for c in WebCase.objects.filter(owner=suite.owner, product=suite.product, pk__in=suite.case_ids)}
    if not suite.case_ids or len(selected) != len(suite.case_ids) or len(rows)*len(suite.case_ids) > 20:
        raise ValueError('用例已删除或归属变化，或数据组数 × 用例数超过 20。请重新保存套件。')
    cases = []
    for index, row in enumerate(rows):
        merged = dict(values, **row)
        expanded_setup = expand_steps(setup, merged) if setup else []
        for pk in suite.case_ids:
            case = selected[pk]
            steps = expand_steps(json.loads(decrypt_api_key(case.steps_encrypted)), merged)
            name = (case.name[:180]+' · 数据组 '+str(index+1)) if len(rows)>1 else case.name
            cases.append({'id':pk,'name':name,'steps':steps,'dataset':index,'setup_steps':expanded_setup})
    return {'base_url':base_url,'ignore_https_errors':ignore_https,'stop_on_failure':suite.stop_on_failure,'cases':cases}
