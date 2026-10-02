import json
import os
from django import forms
from tcms.ai_assistant.crypto import decrypt_api_key, encrypt_api_key
from tcms.management.models import Product
from .models import WebCase, WebSuite, WebEnvironment
from tcms.ai_assistant.automation_data import DatasetField

from .validation import validate_steps, validate_url


class StyledForm(forms.ModelForm):
    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-control"


class CaseForm(StyledForm):
    steps = forms.JSONField(label="操作步骤", widget=forms.Textarea(attrs={"rows": 10}), help_text="支持 CSS、text=文本、role=button[name=名称] 等 Playwright 定位器。")

    class Meta:
        model = WebCase
        fields = ("product", "name", "description", "steps")
        labels = {"product": "所属产品"}
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["steps"] = json.loads(decrypt_api_key(self.instance.steps_encrypted))
        else:
            self.initial.setdefault("steps", [{"action": "goto", "value": "/"}, {"action": "assert_visible", "selector": "body"}])

    def clean_steps(self):
        try:
            return validate_steps(self.cleaned_data["steps"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc

    def clean_product(self):
        product = self.cleaned_data["product"]
        if self.instance.pk and self.instance.product_id != product.pk:
            raise forms.ValidationError("已有用例不能迁移产品，请在目标产品新建用例。")
        return product

    def save(self, commit=True):
        self.instance.owner = self.owner
        self.instance.steps_encrypted = encrypt_api_key(json.dumps(self.cleaned_data["steps"], ensure_ascii=False))
        return super().save(commit)


class SuiteForm(StyledForm):
    datasets = DatasetField()
    cases = forms.ModelMultipleChoiceField(label="包含用例（按用例编号执行）", queryset=WebCase.objects.none(), widget=forms.SelectMultiple(attrs={"size": 10}))

    class Meta:
        model = WebSuite
        fields = ("product", "name", "environment", "base_url", "cases", "datasets", "ignore_https_errors", "stop_on_failure")
        labels = {"product": "所属产品"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cases"].queryset = WebCase.objects.filter(owner=self.owner).select_related("product")
        self.fields["environment"].queryset = WebEnvironment.objects.filter(owner=self.owner)
        self.fields["base_url"].required = False
        self.fields["base_url"].help_text = "选择执行环境后使用环境站点；未选择环境时填写此地址。"
        if self.instance.pk:
            self.initial["datasets"] = json.loads(decrypt_api_key(self.instance.datasets_encrypted) or "[]")
        self.initial.setdefault("base_url", os.environ.get("WEB_TEST_BASE_URL", "https://kiwi-web:8443"))
        if self.instance.pk:
            self.initial["cases"] = self.instance.case_ids

    def clean_base_url(self):
        try:
            if self.cleaned_data.get("environment"):
                return self.cleaned_data["environment"].base_url
            return validate_url(self.cleaned_data["base_url"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc

    def clean(self):
        data = super().clean()
        cases = data.get("cases")
        env = data.get("environment")
        if env and env.product_id != getattr(data.get("product"), "pk", None):
            self.add_error("environment", "环境必须属于所选产品。")
        if cases is not None and cases.count() * max(1, len(data.get("datasets") or [])) > 20:
            self.add_error("datasets", "数据组数 × 用例数不能超过 20。")
        if cases is not None:
            if cases.count() > 20:
                self.add_error("cases", "每个套件最多 20 条用例。")
            if data.get("product") and cases.exclude(product=data["product"]).exists():
                self.add_error("cases", "所有用例必须属于所选产品。")
        return data

    def save(self, commit=True):
        self.instance.owner = self.owner
        self.instance.case_ids = list(self.cleaned_data["cases"].order_by("pk").values_list("pk", flat=True))
        self.instance.datasets_encrypted = encrypt_api_key(json.dumps(self.cleaned_data.get("datasets") or []))
        return super().save(commit)
