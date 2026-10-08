"""Project-scoped publication, structured editing and reproducible AI guidance."""
import copy
from django import forms
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from tcms.management.models import Product
from . import roles
from .models import AIInstructionProfile, AIInstructionRevision, ProjectAIRuleBinding

SECTION_LABELS = (("glossary", "业务术语"), ("focus", "测试重点"),
                  ("conventions", "用例编写规范"), ("boundaries", "禁止事项"))


def can_manage(user, product):
    if not user.is_authenticated or not user.is_active or roles.is_read_only(user):
        return False
    if user.is_superuser:
        return True
    return roles.is_product_member(user, product) and (
        roles.ROLE_MANAGER in roles.roles_of(user)
        or user.has_perm("management.change_product")
        or user.has_perm("management.change_product", product))


def managed_products(user):
    return Product.objects.filter(pk__in=[p.pk for p in roles.member_products(user) if can_manage(user, p)])


def visible_profiles(user):
    from django.db.models import Q
    if user.is_superuser:
        return AIInstructionProfile.objects.all()
    return AIInstructionProfile.objects.filter(Q(owner=user) | Q(
        project_binding__product__in=roles.member_products(user))).distinct()


def snapshot(profile):
    return {key: copy.deepcopy(getattr(profile, key)) for key in (
        "name", "description", "instructions", "sections", "is_active", "operation", "product_id")}


def remember(profile, user=None):
    return AIInstructionRevision.objects.get_or_create(profile=profile, version=profile.version,
        defaults={"snapshot": snapshot(profile), "changed_by": user or profile.owner})[0]


class RuleForm(forms.Form):
    product = forms.ModelChoiceField(label="项目", queryset=Product.objects.none())
    name = forms.CharField(label="规则名称", max_length=100)
    description = forms.CharField(label="说明", max_length=255, required=False)
    operation = forms.ChoiceField(label="适用范围", choices=AIInstructionProfile.OPERATION_CHOICES)
    glossary = forms.CharField(label="业务术语", required=False)
    focus = forms.CharField(label="测试重点", required=False)
    conventions = forms.CharField(label="用例编写规范", required=False)
    boundaries = forms.CharField(label="禁止事项", required=False)
    legacy = forms.CharField(label="原有规则内容", required=False)
    is_active = forms.BooleanField(label="启用规则", required=False, initial=True)
    version = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, user, profile=None, **kwargs):
        self.profile, self.user = profile, user
        initial = kwargs.setdefault("initial", {})
        initial.setdefault("operation", "all")
        if profile:
            initial.update(product=profile.product_id, name=profile.name, description=profile.description,
                operation=profile.operation, is_active=profile.is_active, version=profile.version)
            for key, _ in SECTION_LABELS:
                initial[key] = profile.sections.get(key, "")
            initial["legacy"] = profile.sections.get("legacy", "") if profile.sections else profile.instructions
        super().__init__(*args, **kwargs)
        self.fields["operation"].choices = tuple((key, "需求与测试设计" if key == "all" else label) for key, label in AIInstructionProfile.OPERATION_CHOICES)
        self.fields["product"].queryset = managed_products(user)
        if profile:
            self.fields["product"].disabled = True
        for key, field in self.fields.items():
            if key in dict(SECTION_LABELS) or key == "legacy":
                field.widget = forms.Textarea(attrs={"rows":4, "maxlength":12000})
            if not field.widget.is_hidden and not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-control"
        self.show_legacy = bool(initial.get("legacy") or (self.is_bound and self.data.get("legacy")))

    def clean(self):
        data = super().clean()
        product = data.get("product")
        if product and not can_manage(self.user, product):
            raise forms.ValidationError("没有维护该项目 AI 测试规则的权限。")
        if product and self.profile is None and ProjectAIRuleBinding.objects.filter(product=product).exists():
            raise forms.ValidationError("该项目已有共享规则，请编辑现有规则。")
        sections = {key: (data.get(key) or "").strip() for key in (*dict(SECTION_LABELS), "legacy")}
        instructions = "\n\n".join(f"## {label}\n{sections[key]}" for key, label in (
            *SECTION_LABELS, ("legacy", "原有规则内容")) if sections[key])
        if len(instructions) < 10:
            raise forms.ValidationError("请填写至少一项测试规则，内容至少 10 个字符。")
        if len(instructions) > 12000:
            raise forms.ValidationError("规则总内容不能超过 12000 个字符。")
        data.update(sections=sections, instructions=instructions)
        return data


def save_rule(user, data, profile=None):
    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=data["product"].pk)
        if not can_manage(user, product):
            raise PermissionDenied
        binding = ProjectAIRuleBinding.objects.select_for_update().filter(product=product).first()
        if profile:
            profile = AIInstructionProfile.objects.select_for_update().get(pk=profile.pk)
            if profile.product_id != product.pk or not binding or binding.profile_id != profile.pk:
                raise ValueError("此规则不是该项目发布的共享规则，请从规则列表重新打开。")
            if data.get("version") != profile.version:
                raise ValueError("规则已被他人修改，请刷新页面后再编辑。")
        elif binding:
            raise ValueError("该项目已有共享规则，请编辑现有规则。")
        else:
            # Reuse only this user's legacy row, preserving it as a prior revision.
            profile = AIInstructionProfile.objects.select_for_update().filter(owner=user, product=product).first()
        values = {key: data[key] for key in ("name", "description", "operation", "is_active", "sections", "instructions")}
        if profile:
            remember(profile)
            if any(getattr(profile, key) != value for key, value in values.items()):
                AIInstructionProfile.objects.filter(pk=profile.pk).update(**values, version=profile.version+1, updated=timezone.now())
                profile.refresh_from_db()
        else:
            profile = AIInstructionProfile.objects.create(owner=user, product=product, **values)
        remember(profile, user)
        ProjectAIRuleBinding.objects.get_or_create(product=product,
            defaults={"profile":profile, "published_by":user})
        return profile


def publish_legacy(user, profile):
    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=profile.product_id)
        if not can_manage(user, product) or not (user.is_superuser or profile.owner_id == user.pk):
            raise PermissionDenied
        profile = AIInstructionProfile.objects.select_for_update().get(pk=profile.pk, product=product)
        binding = ProjectAIRuleBinding.objects.filter(product=product).first()
        if binding and binding.profile_id != profile.pk:
            raise ValueError("项目已有共享规则，请编辑现有规则；旧内容不会被覆盖。")
        remember(profile, user)
        return ProjectAIRuleBinding.objects.get_or_create(product=product,
            defaults={"profile":profile, "published_by":user})[0]


def toggle_rule(user, profile, version):
    if profile.product_id is None:
        raise ValueError("请先为规则关联项目并发布。")
    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=profile.product_id)
        if not can_manage(user, product):
            raise PermissionDenied
        profile = AIInstructionProfile.objects.select_for_update().get(pk=profile.pk)
        if not ProjectAIRuleBinding.objects.filter(product=product, profile=profile).exists():
            raise ValueError("请先将旧规则发布为项目共享规则。")
        if str(profile.version) != str(version):
            raise ValueError("规则版本已变化，请刷新后再操作。")
        remember(profile)
        AIInstructionProfile.objects.filter(pk=profile.pk).update(is_active=not profile.is_active,
            version=profile.version+1, updated=timezone.now())
        profile.refresh_from_db()
        remember(profile, user)
        return profile
