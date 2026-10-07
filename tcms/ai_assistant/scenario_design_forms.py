"""Structured business-case editing with explicit raw-text compatibility."""

from django import forms
from django.forms import formset_factory
from tcms.testcases.models import Category, TestCase
from .scenario_design import parse_design, inline_test_data


class StepForm(forms.Form):
    action = forms.CharField(
        label="操作步骤", widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"})
    )
    data = forms.CharField(
        label="输入数据",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
    )
    expected = forms.CharField(
        label="预期结果", widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for item in self.fields.values():
            item.widget.attrs["aria-label"] = item.label


StepFormSet = formset_factory(
    StepForm,
    extra=0,
    min_num=1,
    validate_min=True,
    max_num=100,
    validate_max=True,
    absolute_max=100,
    can_delete=True,
)


class ScenarioForm(forms.ModelForm):
    confirmed = forms.BooleanField(label="我已核对测试目标、前置条件、步骤和预期结果")
    design_mode = forms.ChoiceField(
        label="编辑方式",
        choices=(("structured", "表格编辑"), ("raw", "原文编辑")),
        required=False,
        widget=forms.RadioSelect,
    )
    test_type = forms.CharField(label="测试类型", required=False, max_length=80)
    preconditions = forms.CharField(
        label="前置条件", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    extra = forms.CharField(
        label="补充原文", required=False, widget=forms.Textarea(attrs={"rows": 4})
    )
    case_number = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = TestCase
        fields = ("summary", "category", "priority", "case_status", "requirement", "text", "notes")
        labels = {
            "summary": "用例名称",
            "category": "所属模块",
            "text": "用例原文",
            "notes": "备注",
            "requirement": "关联需求",
            "case_status": "用例状态",
            "priority": "优先级",
        }
        widgets = {
            "text": forms.Textarea(attrs={"rows": 14}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, product, **kwargs):
        super().__init__(*args, **kwargs)
        self.design = parse_design(self.instance.text)
        self.fields["category"].queryset = Category.objects.filter(product=product)
        self.mode = (
            self.data.get("design_mode", "raw")
            if self.is_bound
            else "structured" if not self.instance.pk or self.design.structured else "raw"
        )
        self.initial["design_mode"] = self.mode
        self.design = inline_test_data(self.design)
        self.fields["text"].required = self.mode != "structured"
        self.fields["text"].help_text = ""
        for key in ("test_type", "preconditions", "extra", "case_number"):
            self.initial[key] = getattr(self.design, key)
        for item in self.fields.values():
            if not isinstance(
                item.widget, (forms.CheckboxInput, forms.RadioSelect, forms.HiddenInput)
            ):
                item.widget.attrs["class"] = "form-control"

    def clean_notes(self):
        # Older clients posted only native text; do not erase an existing note.
        if "notes" not in self.data:
            return self.instance.notes
        return self.cleaned_data["notes"]

    def clean_requirement(self):
        if "requirement" not in self.data:
            return self.instance.requirement
        return self.cleaned_data["requirement"]
