# -*- coding: utf-8 -*-
from django import forms
from django.db.models import Q
from guardian.shortcuts import get_objects_for_user
from django.forms import inlineformset_factory

from tcms.core.forms.fields import UserField
from tcms.core.widgets import SimpleMDE
from tcms.management.models import Product, Version
from tcms.testplans.models import TestPlan, TestPlanEmailSettings


class ParentPlanSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if value and hasattr(value, "instance"):
            option["attrs"]["data-product"] = value.instance.product_id
        return option


class NewPlanForm(forms.ModelForm):
    class Meta:
        model = TestPlan
        exclude = ("tag",)  # pylint: disable=modelform-uses-exclude

    text = forms.CharField(widget=SimpleMDE(), required=False)

    def __init__(self, *args, **kwargs):
        request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        if request and hasattr(request, "tenant") and request.tenant.schema_name != "public":
            self.fields["author"].queryset = request.tenant.authorized_users.all()

        self.private_parent_id = None
        parents = TestPlan.objects.select_related("product_version").order_by("name", "pk")
        if request:
            visible = get_objects_for_user(request.user, "testplans.view_testplan", klass=TestPlan)
            current = self.instance.parent_id if self.instance.pk else None
            if current and not visible.filter(pk=current).exists():
                self.private_parent_id = current
            # Preserve an existing relationship without exposing a parent's
            # title when only this child plan can be edited.
            parents = parents.filter(Q(pk__in=visible.values("pk")) | Q(pk=current))
        if self.instance.pk:
            parents = parents.exclude(
                pk__in=self.instance.descendants(include_self=True).values("pk")
            )
        self.fields["parent"].queryset = parents
        self.fields["parent"].empty_label = "无上级计划"
        self.fields["parent"].widget = ParentPlanSelect(
            attrs={"class": "form-control", "id": "id_parent"}
        )
        self.fields["parent"].label_from_instance = lambda plan: (
            "当前上级计划（无查看权限）"
            if plan.pk == self.private_parent_id
            else f"TP-{plan.pk} · {plan.name} · {plan.product_version.value}"
        )
        self.fields["parent"].widget.choices = self.fields["parent"].choices

    def clean(self):
        data = super().clean()
        parent, product = data.get("parent"), data.get("product")
        if parent and product and parent.product_id != product.pk:
            self.add_error("parent", "父级计划必须属于同一项目。")
        return data

    def populate(self, product_id):
        if product_id:
            self.fields["product_version"].queryset = Version.objects.filter(product_id=product_id)
        else:
            self.fields["product_version"].queryset = Version.objects.all()


# note: these fields can't change during runtime !
_email_settings_fields = []  # pylint: disable=invalid-name
for field in TestPlanEmailSettings._meta.fields:
    _email_settings_fields.append(field.name)


# for usage in CreateView, UpdateView
PlanNotifyFormSet = inlineformset_factory(  # pylint: disable=invalid-name
    TestPlan,
    TestPlanEmailSettings,
    fields=_email_settings_fields,
    can_delete=False,
    can_order=False,
)


class SearchPlanForm(forms.ModelForm):
    class Meta:
        model = TestPlan
        fields = "__all__"

    # overriden widget
    author = UserField()

    # extra fields
    default_tester = UserField()

    def populate(self, product_id=None):
        if product_id:
            self.fields["product_version"].queryset = Version.objects.filter(product_id=product_id)
        else:
            self.fields["product_version"].queryset = Version.objects.none()


class ClonePlanForm(forms.Form):  # pylint: disable=must-inherit-from-model-form
    name = forms.CharField(required=True)

    product = forms.ModelChoiceField(
        queryset=Product.objects.all(),
        empty_label=None,
    )
    version = forms.ModelChoiceField(
        queryset=Version.objects.none(),
        empty_label=None,
    )

    copy_testcases = forms.BooleanField(required=False)
    set_parent = forms.BooleanField(required=False)

    def populate(self, product_pk):
        if product_pk:
            self.fields["version"].queryset = Version.objects.filter(product_id=product_pk)
        else:
            self.fields["version"].queryset = Version.objects.none()
