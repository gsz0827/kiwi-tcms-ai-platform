import json
import re
import uuid

from django import forms
from guardian.shortcuts import get_objects_for_user

from tcms.testcases.models import TestCase, Category
from tcms.testruns.models import TestExecutionStatus, TestRun

from .api_validation import validate_case, validate_destination, validate_headers
from .crypto import encrypt_api_key
from .models import APICase, APIEnvironment, APISuite


class StyledForm:
    def style_fields(self):
        for field in self.fields.values():
            if not isinstance(field.widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
                field.widget.attrs["class"] = "form-control"
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs["rows"] = 3


class EnvironmentForm(StyledForm, forms.ModelForm):
    secret_headers = forms.JSONField(
        required=False, label="认证请求头（加密保存）",
        help_text='例如 {"Authorization":"Bearer 你的测试令牌"}。保存后不回显，编辑时留空保留。',
        widget=forms.Textarea(attrs={"autocomplete": "off"}),
    )
    clear_secrets = forms.BooleanField(required=False, label="清除已保存的认证请求头")

    class Meta:
        model = APIEnvironment
        fields = ("name", "base_url", "headers", "variables", "timeout")
        help_texts = {
            "base_url": "例如 http://api-demo:8080。地址由后台 Worker 访问，应填写 Worker 可访问的测试服务。",
            "variables": '普通参数，例如 {"user_id":1}；用例中使用 {{user_id}} 引用。认证令牌请放在认证请求头中。',
            "headers": '普通请求头，例如 {"Accept":"application/json"}。',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()
        self.fields["timeout"].min_value = 1
        self.fields["timeout"].max_value = 30

    def clean(self):
        data = super().clean()
        try:
            if data.get("base_url"):
                validate_destination(data["base_url"])
            for key in ("headers", "secret_headers"):
                data[key] = data.get(key) or {}
                validate_headers(data[key])
            data["variables"] = data.get("variables") or {}
            if (not isinstance(data["variables"], dict) or any(
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or not isinstance(value, (str, int, float, bool))
                for key, value in data["variables"].items()
            )):
                raise ValueError("环境变量应是 JSON 对象，变量名由字母、数字、下划线组成，值为简单类型。")
            if data.get("timeout") is not None and not 1 <= data["timeout"] <= 30:
                raise ValueError("超时应在 1 到 30 秒之间。")
            if len(json.dumps(data, default=str, allow_nan=False).encode()) > 32768:
                raise ValueError("环境配置不能超过 32 KB。")
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.cleaned_data["clear_secrets"]:
            instance.secret_headers_encrypted = ""
        if self.cleaned_data["secret_headers"]:
            instance.secret_headers_encrypted = encrypt_api_key(
                json.dumps(self.cleaned_data["secret_headers"])
            )
        if commit:
            instance.save()
        return instance


class APICaseForm(StyledForm, forms.ModelForm):
    class Meta:
        model = APICase
        labels = {"name": "接口配置名称"}
        fields = ("name", "sequence", "method", "path", "query", "headers", "send_body", "body",
                  "expected_status", "assertions", "extracts", "max_elapsed_ms", "test_case")
        help_texts = {
            "sequence": "数值越小越先执行，相同时按创建顺序。登录等前置用例应排在前面。",
            "extracts": '例如 {"access_token":"token"}；后续请求头可填写 {"Authorization":"Bearer {{access_token}}"}。仅本次运行有效，提取值不展示。',
            "path": "例如 /users/{{user_id}}。只填写路径，服务地址来自执行环境。",
            "query": '例如 {"page":1}，不需要时填写 {}。',
            "assertions": '[{"path":"data.id","operator":"equals","expected":1}]；exists 检查字段存在；数组路径示例 data.0.id。',
            "max_elapsed_ms": "0 表示不检查耗时。",
            "test_case": "选择用例库中的业务用例；留空时会在用例库自动创建一条自动化用例。关联后可回写已有测试运行。",
        }

    def __init__(self, *args, owner, product, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner
        self.fields["test_case"].queryset = get_objects_for_user(
            owner, "testcases.change_testcase", klass=TestCase
        ).filter(category__product=product)
        self.style_fields()
        if not self.instance.pk and self.initial.get("test_case"):
            selected = self.fields["test_case"].queryset.filter(pk=self.initial["test_case"]).first() if str(self.initial["test_case"]).isdigit() else None
            if selected:
                self.initial["name"] = self.initial.get("name") or selected.summary[:200]

    def clean(self):
        data = super().clean()
        if not data.get("test_case") and not self.owner.has_perm("testcases.add_testcase"):
            self.add_error("test_case", "请选择已有测试用例，或向管理员申请新建用例权限。")
        for field, default in (("headers", {}), ("query", {}), ("body", {}), ("assertions", []), ("extracts", {})):
            if data.get(field) is None:
                data[field] = default
        if not self.errors:
            try:
                validate_case({key: value for key, value in data.items() if key != "test_case"})
            except ValueError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return data


class APISubmitForm(StyledForm, forms.Form):
    submission_token = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    environment = forms.ModelChoiceField(queryset=APIEnvironment.objects.none(), label="执行环境")
    cases = forms.ModelMultipleChoiceField(
        queryset=APICase.objects.none(), label="要执行的接口用例（最多 20 条，按执行顺序）",
        widget=forms.CheckboxSelectMultiple,
    )
    stop_on_failure = forms.BooleanField(
        required=False, label="遇到失败或请求异常时停止后续用例",
        help_text="不勾选则继续独立用例；缺少前置提取变量的用例始终跳过，不发送请求。",
    )
    share_cookies = forms.BooleanField(
        required=False, label="在本次执行中共享登录 Cookie",
        help_text="前序响应设置的 Cookie 自动用于后续匹配的请求，每次运行重新建立会话。显式填写的 Cookie 请求头优先。",
    )
    test_run = forms.ModelChoiceField(
        queryset=TestRun.objects.none(), required=False, label="回写到已有测试运行（可选）",
        help_text="所选接口用例必须关联该运行中唯一的一条测试执行，运行需要处于未结束状态。",
    )
    passed_status = forms.ModelChoiceField(
        queryset=TestExecutionStatus.objects.filter(weight__gt=0), required=False,
        label="通过时回写的状态",
    )
    failed_status = forms.ModelChoiceField(
        queryset=TestExecutionStatus.objects.filter(weight__lt=0), required=False,
        label="断言失败时回写的状态",
    )

    def __init__(self, *args, owner, product, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["environment"].queryset = APIEnvironment.objects.filter(owner=owner, product=product)
        self.fields["cases"].queryset = APICase.objects.filter(owner=owner, product=product).order_by("sequence", "pk")
        self.fields["cases"].label_from_instance = lambda case: f"{case.sequence} · {case}"
        if owner.has_perm("testruns.change_testexecution"):
            self.fields["test_run"].queryset = get_objects_for_user(
                owner, "testruns.change_testrun", klass=TestRun
            ).filter(plan__product=product, stop_date__isnull=True)
        self.style_fields()

    def clean(self):
        data = super().clean()
        if len(data.get("cases", [])) > 20:
            raise forms.ValidationError("一次最多执行 20 条用例。")
        if data.get("test_run") and not (data.get("passed_status") and data.get("failed_status")):
            raise forms.ValidationError("回写测试运行时，请选择通过和断言失败对应的状态。")
        return data


class SuiteForm(StyledForm, forms.ModelForm):
    cases = forms.ModelMultipleChoiceField(queryset=APICase.objects.none(),
        label="自动化用例（最多 20 条）", widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = APISuite
        fields = ("name", "environment", "cases", "stop_on_failure", "share_cookies",
                  "schedule_enabled", "interval_minutes", "next_run_at")
        labels = {"environment": "执行环境"}
        widgets = {"next_run_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")}
        help_texts = {
            "interval_minutes": "5～10080 分钟；60 为每小时，1440 为每天。前次未完成时跳过本轮。",
            "next_run_at": "首次时间可留空，默认一个间隔后开始；页面按当前站点时区显示。停机期间遗漏的执行不会补跑。",
            "share_cookies": "仅同一次运行共享，遵循 Cookie 的路径、域名、有效期和 Secure 限制。",
        }

    def __init__(self, *args, owner, product, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["environment"].queryset = APIEnvironment.objects.filter(owner=owner, product=product)
        self.fields["cases"].queryset = APICase.objects.filter(owner=owner, product=product).order_by("sequence", "pk")
        if self.instance.pk:
            self.initial["cases"] = self.instance.case_ids
        self.style_fields()

    def clean(self):
        data = super().clean()
        if len(data.get("cases", [])) > 20:
            self.add_error("cases", "最多选择 20 条用例。")
        if not 5 <= (data.get("interval_minutes") or 0) <= 10080:
            self.add_error("interval_minutes", "间隔须为 5～10080 分钟。")
        return data


class LibraryCaseForm(StyledForm, forms.ModelForm):
    execution_type = forms.ChoiceField(label="执行方式", choices=[("manual", "手工"), ("api", "自动化 · 接口")])

    class Meta:
        model = TestCase
        fields = ("summary", "category", "priority", "case_status", "requirement", "text")
        labels = {"summary": "用例标题", "category": "业务分类", "priority": "优先级",
                  "case_status": "用例状态", "requirement": "关联需求", "text": "前置条件、步骤与预期结果"}

    def __init__(self, *args, product, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(product=product)
        self.style_fields()
