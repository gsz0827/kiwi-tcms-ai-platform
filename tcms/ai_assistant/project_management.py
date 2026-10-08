"""Project administration without leaking private AI/model configuration."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponseBadRequest
from django.views.decorators.http import require_http_methods
from tcms.management.models import Product, Version, Build
from . import roles
from .project_rules import can_manage


class ProjectForm(forms.ModelForm):
    revision = forms.IntegerField(widget=forms.HiddenInput)

    class Meta:
        model = Product
        fields = ("name", "description")
        labels = {"name": "项目名称", "description": "项目说明"}
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}


class VersionForm(forms.Form):
    value = forms.CharField(label="版本名称", max_length=192)


class BuildForm(forms.Form):
    version = forms.ModelChoiceField(label="所属版本", queryset=Version.objects.none())
    name = forms.CharField(label="构建名称", max_length=255)


def style(form):
    for field in form.fields.values():
        field.widget.attrs["class"] = "form-control"
    return form


@login_required
@require_http_methods(["GET"])
def listing(request):
    products = roles.member_products(request.user).prefetch_related("version")
    query = request.GET.get("q", "").strip()[:255]
    if query:
        products = products.filter(name__icontains=query)
    return render(request, "ai_assistant/project_settings.html", {
        "projects": products, "query": query,
        "can_create": request.user.has_perm("management.add_product") and not roles.is_read_only(request.user),
    })


@login_required
@require_http_methods(["GET", "POST"])
def detail(request, pk):
    product = get_object_or_404(roles.member_products(request.user), pk=pk)
    editable = can_manage(request.user, product)
    revision = product.history.latest().history_id
    action = request.POST.get("action") if request.method == "POST" else None
    project_form = style(ProjectForm(request.POST if action == "project" else None,
        instance=product, initial={"revision": revision}))
    version_form = style(VersionForm(request.POST if action == "version" else None))
    build_form = style(BuildForm(request.POST if action == "build" else None))
    build_form.fields["version"].queryset = product.version.all()
    if request.method == "POST":
        if not editable:
            raise PermissionDenied
        form = {"project": project_form, "version": version_form, "build": build_form}.get(action)
        if form is None:
            return HttpResponseBadRequest("未知项目操作。")
        if form.is_valid():
            try:
                with transaction.atomic():
                    locked = Product.objects.select_for_update().get(pk=product.pk)
                    if not can_manage(request.user, locked):
                        raise PermissionDenied
                    if action == "project":
                        if locked.history.latest().history_id != form.cleaned_data["revision"]:
                            raise ValueError("项目信息已被修改，请刷新页面后再编辑。")
                        locked.name = form.cleaned_data["name"]
                        locked.description = form.cleaned_data["description"]
                        locked.save(update_fields=("name", "description"))
                    elif action == "version":
                        if Version.objects.filter(product=locked, value=form.cleaned_data["value"]).exists():
                            raise ValueError("这个版本名称已经存在。")
                        Version.objects.create(product=locked, value=form.cleaned_data["value"])
                    else:
                        version = get_object_or_404(Version, product=locked, pk=form.cleaned_data["version"].pk)
                        if Build.objects.filter(version=version, name=form.cleaned_data["name"]).exists():
                            raise ValueError("这个版本下已有同名构建。")
                        Build.objects.create(version=version, name=form.cleaned_data["name"])
                messages.success(request, "项目配置已保存。")
                return redirect("ai_assistant:project_detail", pk=pk)
            except (ValueError, IntegrityError) as exc:
                form.add_error(None, str(exc) if isinstance(exc, ValueError) else "名称已存在，请更换名称。")
    return render(request, "ai_assistant/project_detail.html", {
        "project": product, "editable": editable, "project_form": project_form,
        "version_form": version_form, "build_form": build_form,
        "versions": product.version.prefetch_related("build"),
        "members": roles.members_of_product(product),
        "can_manage_members": roles.can_manage_members(request.user),
    })
