import uuid

from django import forms

from tcms.management.models import Product, Version
from tcms.testcases.models import Category
from tcms.testruns.models import TestRun

from .crypto import encrypt_api_key
from .models import (
    AIDefectDraft,
    AIIterationReport,
    AIInstructionProfile,
    AIModelConfig,
    AIReleaseGateRule,
    AIRequest,
    AITestCaseDraft,
    AITestReport,
)


class CategoryChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.product.name} / {obj.name}"


class AIRequestForm(forms.ModelForm):
    submission_token = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    category = CategoryChoiceField(
        queryset=Category.objects.select_related("product").order_by(
            "product__name", "name"
        ),
        empty_label="请选择产品 / 分类",
        label="目标分类",
        widget=forms.Select(attrs={"class": "form-control"}),
    )

    class Meta:
        model = AIRequest
        fields = ("category", "title", "requirement")
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "例如：手机号验证码登录",
                }
            ),
            "requirement": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 6,
                    "placeholder": "请输入需求描述、业务规则和验收标准",
                }
            ),
        }


class AITestCaseDraftForm(forms.ModelForm):
    preconditions_text = forms.CharField(
        required=False,
        label="前置条件",
        help_text="每行填写一项前置条件。",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4}),
    )
    steps_text = forms.CharField(
        label="测试步骤",
        help_text="每行一项，格式：操作 => 预期结果。",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 8}),
    )

    class Meta:
        model = AITestCaseDraft
        fields = ("case_number", "summary", "priority", "test_type")
        widgets = {
            "case_number": forms.TextInput(attrs={"class": "form-control"}),
            "summary": forms.TextInput(attrs={"class": "form-control"}),
            "priority": forms.Select(
                choices=[(f"P{i}", f"P{i}") for i in range(1, 6)],
                attrs={"class": "form-control"},
            ),
            "test_type": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["preconditions_text"].initial = "\n".join(
                self.instance.preconditions or []
            )
            self.fields["steps_text"].initial = "\n".join(
                f"{step.get('action', '')} => {step.get('expected', '')}"
                for step in self.instance.steps or []
            )

    def clean_preconditions_text(self):
        return [
            line.strip()
            for line in self.cleaned_data["preconditions_text"].splitlines()
            if line.strip()
        ]

    def clean_steps_text(self):
        steps = []
        for line_number, raw_line in enumerate(
            self.cleaned_data["steps_text"].splitlines(), start=1
        ):
            line = raw_line.strip()
            if not line:
                continue
            separator = "=>" if "=>" in line else "→" if "→" in line else None
            if separator:
                action, expected = (part.strip() for part in line.split(separator, 1))
            else:
                action, expected = line, ""
            if not action:
                raise forms.ValidationError(f"第 {line_number} 行缺少操作步骤")
            steps.append({"action": action, "expected": expected})
        if not steps:
            raise forms.ValidationError("请至少填写一个测试步骤")
        return steps

    def save(self, commit=True):
        draft = super().save(commit=False)
        draft.preconditions = self.cleaned_data["preconditions_text"]
        draft.steps = self.cleaned_data["steps_text"]
        if commit:
            draft.save()
        return draft


class AIModelConfigForm(forms.ModelForm):
    api_key = forms.CharField(
        required=False,
        label="API 密钥",
        help_text="密钥只会加密保存且不会回显；编辑时留空表示保留原密钥。",
        widget=forms.PasswordInput(
            attrs={"class": "form-control", "autocomplete": "new-password"}
        ),
    )

    class Meta:
        model = AIModelConfig
        fields = ("name", "api_base", "model", "timeout", "is_active")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "api_base": forms.URLInput(
                attrs={"class": "form-control", "placeholder": "https://api.example.com/v1"}
            ),
            "model": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "模型标识"}
            ),
            "timeout": forms.NumberInput(
                attrs={"class": "form-control", "min": 10, "max": 600}
            ),
        }

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner
        self.instance.owner = owner

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        duplicate = AIModelConfig.objects.filter(owner=self.owner, name=name).exclude(
            pk=self.instance.pk
        )
        if duplicate.exists():
            raise forms.ValidationError("你已经有一个同名模型配置")
        return name

    def clean_api_key(self):
        api_key = self.cleaned_data["api_key"].strip()
        if not api_key and not self.instance.api_key_encrypted:
            raise forms.ValidationError("新建模型配置时必须填写 API 密钥")
        return api_key

    def clean_timeout(self):
        timeout = self.cleaned_data["timeout"]
        if not 10 <= timeout <= 600:
            raise forms.ValidationError("超时时间必须在 10 到 600 秒之间")
        return timeout

    def save(self, commit=True):
        config = super().save(commit=False)
        config.owner = self.owner
        if self.cleaned_data["api_key"]:
            config.api_key_encrypted = encrypt_api_key(self.cleaned_data["api_key"])
        if commit:
            config.save()
        return config


class AIInstructionProfileForm(forms.ModelForm):
    class Meta:
        model = AIInstructionProfile
        fields = ("name", "description", "product", "instructions", "is_active")
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "例如：支付需求测试规范"}
            ),
            "description": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "这组规则解决什么问题"}
            ),
            "product": forms.Select(attrs={"class": "form-control"}),
            "instructions": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 12,
                    "placeholder": "每行写一条可执行的测试分析规则或领域知识。",
                }
            ),
            "is_active": forms.CheckboxInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner
        self.instance.owner = owner
        self.fields["product"].queryset = Product.objects.order_by("name")
        self.fields["product"].label = "适用项目（Kiwi 产品）"
        self.fields["product"].required = True

    def clean(self):
        cleaned_data = super().clean()
        product = cleaned_data.get("product")
        duplicate = AIInstructionProfile.objects.filter(
            owner=self.owner, product=product
        ).exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise forms.ValidationError("一个项目只能绑定一个 AI 规则包")
        return cleaned_data

    def clean_instructions(self):
        instructions = self.cleaned_data["instructions"].strip()
        if len(instructions) < 10:
            raise forms.ValidationError("测试规则至少需要 10 个字符")
        if len(instructions) > 12000:
            raise forms.ValidationError("测试规则不能超过 12000 个字符")
        return instructions

    def save(self, commit=True):
        profile = super().save(commit=False)
        profile.owner = self.owner
        profile.operation = "all"
        if commit:
            profile.save()
        return profile


class LineListMixin:
    @staticmethod
    def _lines(value):
        return [line.strip() for line in value.splitlines() if line.strip()]


class AIDefectDraftForm(LineListMixin, forms.ModelForm):
    preconditions_text = forms.CharField(
        required=False,
        label="前置条件",
        help_text="每行一项。",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4}),
    )
    reproduction_steps_text = forms.CharField(
        required=False,
        label="复现步骤",
        help_text="每行一个步骤，可全程编辑。",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 7}),
    )
    evidence_text = forms.CharField(
        required=False,
        label="直接证据",
        help_text="只填写已经观察到的事实，每行一项。",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4}),
    )
    likely_causes_text = forms.CharField(
        required=False,
        label="待验证的可能原因",
        help_text="这些内容不是已确认根因，每行一项。",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4}),
    )

    class Meta:
        model = AIDefectDraft
        fields = (
            "title",
            "severity",
            "priority",
            "status",
            "fix_version",
            "assignee_name",
            "description",
            "expected_result",
            "actual_result",
            "environment",
            "closure_reason",
            "duplicate_of",
        )
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "severity": forms.Select(attrs={"class": "form-control"}),
            "priority": forms.Select(attrs={"class": "form-control"}),
            "status": forms.Select(attrs={"class": "form-control"}),
            "fix_version": forms.TextInput(attrs={"class": "form-control"}),
            "assignee_name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 5}),
            "expected_result": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "actual_result": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "environment": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "closure_reason": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "duplicate_of": forms.Select(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["duplicate_of"].queryset = AIDefectDraft.objects.filter(
                owner=self.instance.owner
            ).exclude(pk=self.instance.pk)
            self.fields["preconditions_text"].initial = "\n".join(
                self.instance.preconditions or []
            )
            self.fields["reproduction_steps_text"].initial = "\n".join(
                self.instance.reproduction_steps or []
            )
            self.fields["evidence_text"].initial = "\n".join(
                self.instance.evidence or []
            )
            self.fields["likely_causes_text"].initial = "\n".join(
                self.instance.likely_causes or []
            )

    def clean_preconditions_text(self):
        return self._lines(self.cleaned_data["preconditions_text"])

    def clean_reproduction_steps_text(self):
        return self._lines(self.cleaned_data["reproduction_steps_text"])

    def clean_evidence_text(self):
        return self._lines(self.cleaned_data["evidence_text"])

    def clean_likely_causes_text(self):
        return self._lines(self.cleaned_data["likely_causes_text"])

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("status") == "closed" and not cleaned.get("closure_reason", "").strip():
            self.add_error("closure_reason", "关闭缺陷时必须填写关闭原因")
        return cleaned

    def save(self, commit=True):
        draft = super().save(commit=False)
        draft.preconditions = self.cleaned_data["preconditions_text"]
        draft.reproduction_steps = self.cleaned_data["reproduction_steps_text"]
        draft.evidence = self.cleaned_data["evidence_text"]
        draft.likely_causes = self.cleaned_data["likely_causes_text"]
        if commit:
            draft.save()
        return draft


class DefectLinkForm(forms.Form):
    name = forms.CharField(
        max_length=64,
        label="缺陷编号或名称",
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "例如：BUG-1024"}
        ),
    )
    url = forms.URLField(
        label="缺陷地址",
        widget=forms.URLInput(
            attrs={"class": "form-control", "placeholder": "https://..."}
        ),
    )


class AITestReportForm(LineListMixin, forms.ModelForm):
    change_reason = forms.CharField(
        label="本次修改原因",
        help_text="将写入不可删除的报告修订历史。",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    recommendations_text = forms.CharField(
        required=False,
        label="后续建议",
        help_text="每行一项。",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 6}),
    )

    class Meta:
        model = AITestReport
        fields = (
            "title", "summary", "scope", "conclusion", "release_decision",
            "change_reason",
        )
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "summary": forms.Textarea(attrs={"class": "form-control", "rows": 5}),
            "scope": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "conclusion": forms.Textarea(attrs={"class": "form-control", "rows": 5}),
            "release_decision": forms.Select(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["recommendations_text"].initial = "\n".join(
                self.instance.recommendations or []
            )

    def clean_recommendations_text(self):
        return self._lines(self.cleaned_data["recommendations_text"])

    def save(self, commit=True):
        report = super().save(commit=False)
        report.recommendations = self.cleaned_data["recommendations_text"]
        if commit:
            report.save()
        return report


class RegressionVerificationForm(forms.Form):
    regression_run_id = forms.IntegerField(
        min_value=1,
        label="回归测试运行 ID",
        help_text="填写修复后重新执行的 TestRun 编号。",
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    notes = forms.CharField(
        required=False,
        label="人工备注",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )


class ReportApprovalForm(forms.Form):
    decision = forms.ChoiceField(
        choices=(("approved", "批准并签字"), ("rejected", "驳回")),
        label="审批结论",
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    comment = forms.CharField(
        required=False,
        label="审批意见",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )


class AIReleaseGateRuleForm(forms.ModelForm):
    class Meta:
        model = AIReleaseGateRule
        fields = (
            "product", "name", "block_priority", "min_success_rate",
            "require_all_executed", "max_open_defects", "is_active",
        )
        widgets = {
            "product": forms.Select(attrs={"class": "form-control"}),
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "block_priority": forms.Select(attrs={"class": "form-control"}),
            "min_success_rate": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "max": 100, "step": "0.01"}
            ),
            "max_open_defects": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner
        self.fields["product"].queryset = Product.objects.order_by("name")

    def clean_min_success_rate(self):
        value = self.cleaned_data["min_success_rate"]
        if value < 0 or value > 100:
            raise forms.ValidationError("成功率必须在 0 到 100 之间")
        return value

    def clean(self):
        cleaned = super().clean()
        name = cleaned.get("name")
        product = cleaned.get("product")
        if name:
            duplicate = AIReleaseGateRule.objects.filter(
                owner=self.owner, name=name, product=product
            ).exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error("name", "当前账号在该产品范围内已有同名门禁规则")
        return cleaned


class IterationReportForm(forms.Form):
    title = forms.CharField(
        max_length=255, label="迭代报告标题",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    product = forms.ModelChoiceField(
        queryset=Product.objects.none(), label="产品",
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    version = forms.ModelChoiceField(
        queryset=Version.objects.none(), required=False, label="版本",
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    run_ids = forms.CharField(
        label="TestRun ID",
        help_text="填写多个 TestRun 编号，用逗号分隔。",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "101, 102, 103"}),
    )
    conclusion = forms.CharField(
        required=False, label="测试结论",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.order_by("name")
        self.fields["version"].queryset = Version.objects.select_related("product").order_by(
            "product__name", "value"
        )

    def clean_run_ids(self):
        raw = self.cleaned_data["run_ids"].replace("，", ",")
        try:
            ids = list(dict.fromkeys(int(item.strip()) for item in raw.split(",") if item.strip()))
        except ValueError as exc:
            raise forms.ValidationError("TestRun ID 必须是用逗号分隔的整数") from exc
        if not ids:
            raise forms.ValidationError("请至少填写一个 TestRun ID")
        return ids

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get("product")
        version = cleaned.get("version")
        if product and version and version.product_id != product.pk:
            self.add_error("version", "所选版本不属于该产品")
        return cleaned


class RequirementChangeForm(forms.ModelForm):
    change_summary = forms.CharField(
        max_length=255, label="变更说明",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    class Meta:
        model = AIRequest
        fields = ("title", "requirement", "change_summary")
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "requirement": forms.Textarea(attrs={"class": "form-control", "rows": 8}),
        }
