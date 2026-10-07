"""Form-only fields backed by JSON, with explicit grouping and safe legacy POSTs."""

from django import forms
from tcms.management.models import Version
from tcms.testcases.models import Category
from . import roles
from .document_sections import REQUIREMENT_SECTIONS, TASK_SECTIONS, LIST_FIELDS


class SectionFormMixin:
    section_definitions = REQUIREMENT_SECTIONS
    primary_names = (
        "title",
        "background",
        "impact_scope",
        "requirement",
        "business_rules",
        "exceptions",
        "acceptance_criteria",
    )
    attribute_names = ("category", "target_version", "assigned_to", "priority", "status")
    preserve_names = ("target_version", "assigned_to", "priority", "status")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        stored = self.instance.document_sections or {}
        if self.is_bound:
            self.data = self.data.copy()
        for name, label, optional, placeholder in self.section_definitions:
            self.fields[name] = forms.CharField(
                required=False,
                label=label,
                widget=forms.Textarea(
                    attrs={
                        "class": "form-control",
                        "rows": 3 if name in LIST_FIELDS else 4,
                        "placeholder": placeholder,
                    }
                ),
            )
            self.initial[name] = stored.get(name, "")
            if self.is_bound and name not in self.data:
                self.data[name] = stored.get(name, "")
        for name in self.preserve_names:
            if self.is_bound and name not in self.data and name in self.fields:
                self.data[name] = self.initial.get(name, getattr(self.instance, name, None)) or ""

    @property
    def document_fields(self):
        return [self[name] for name in self.primary_names if name in self.fields]

    @property
    def optional_document_fields(self):
        return [
            self[name]
            for name, _, optional, _ in self.section_definitions
            if optional and name in self.fields
        ]

    @property
    def optional_document_open(self):
        return any(field.errors or field.value() for field in self.optional_document_fields)

    @property
    def document_attribute_fields(self):
        return [self[name] for name in self.attribute_names if name in self.fields]

    def clean(self):
        cleaned = super().clean()
        sections = dict(self.instance.document_sections or {})
        for name, _, _, _ in self.section_definitions:
            if name in self.fields and name in cleaned:
                if cleaned[name]:
                    sections[name] = cleaned[name]
                else:
                    sections.pop(name, None)
        cleaned["document_sections"] = sections
        product = getattr(self, "document_product", None)
        version = cleaned.get("target_version")
        if version and (not product or version.product_id != product.pk):
            self.add_error("target_version", "目标版本不属于当前项目。")
        return cleaned

    def save(self, commit=True):
        result = super().save(commit=False)
        result.document_sections = self.cleaned_data["document_sections"]
        if commit:
            result.save()
            self.save_m2m()
        return result


class RequirementSectionFormMixin(SectionFormMixin):
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.document_product = None
        category_id = (
            self.data.get("category")
            if self.is_bound and "category" in self.fields
            else self.instance.category_id
        )
        if category_id:
            try:
                self.document_product = (
                    Category.objects.select_related("product").get(pk=category_id).product
                )
            except (Category.DoesNotExist, ValueError, TypeError):
                pass
        self.fields["target_version"].queryset = (
            Version.objects.filter(product=self.document_product)
            if self.document_product
            else Version.objects.none()
        )
        self.fields["target_version"].empty_label = "未指定目标版本"
        visible_product = user is not None and user.is_authenticated and self.document_product is not None and (roles.member_products(user).filter(pk=self.document_product.pk).exists() or roles.visible_requests(user).filter(category__product=self.document_product).exists())
        self.fields['assigned_to'].queryset = roles.assignable_users(self.document_product if visible_product else None).order_by('username')
        self.fields["assigned_to"].empty_label = "未指派"
        for name in ("target_version", "assigned_to", "priority", "status"):
            self.fields[name].widget.attrs["class"] = "form-control"
