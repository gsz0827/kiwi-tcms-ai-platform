"""Native business-case creation in a visible same-product shared directory."""

from django import forms
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from tcms.management.models import Product
from . import roles
from .case_directories import visible_folders, folder_types
from .models import ProjectResourceAssignment
from .product_case_tree import folder_paths


def can_create(user):
    return (
        user.is_authenticated
        and user.is_active
        and not roles.is_read_only(user)
        and user.has_perm("testcases.add_testcase")
    )


def new_url(product, folder=None):
    url = reverse("ai_assistant:scenario_new", args=[product.pk])
    return url + f"?folder={folder.pk}" if folder else url


def folders_for(user, product):
    return (
        visible_folders(user)
        .filter(product=product, resource_type__in=folder_types("case"))
        .select_related("product")
        .order_by("position", "name", "pk")
    )


def add_directory_field(form, request, product):
    folders = folders_for(request.user, product)
    paths = folder_paths(list(folders))
    field = forms.ModelChoiceField(
        queryset=folders,
        label="保存目录",
        required=False,
        empty_label=product.name + "（根目录）",
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    field.label_from_instance = lambda folder: paths[folder.pk]
    form.fields["folder"] = field
    if request.method != "POST" and request.GET.get("folder"):
        raw = request.GET["folder"]
        folder = (
            get_object_or_404(folders, pk=int(raw))
            if raw.isascii() and raw.isdecimal() and len(raw) <= 18
            else None
        )
        if folder is None:
            from django.http import Http404

            raise Http404
        form.initial["folder"] = folder.pk


def assign_created_case(request, case, folder):
    """Caller holds the creation transaction; folder errors roll the case back."""
    if not folder:
        return
    # Folder edits use the same product lock. Re-check visibility/scope after
    # acquiring it so a deleted or stale directory cannot produce an orphan case.
    Product.objects.select_for_update().get(pk=case.category.product_id)
    current = folders_for(request.user, case.category.product).filter(pk=folder.pk).first()
    if current is None:
        raise ValueError("保存目录已变更或不可访问，请重新选择目录。")
    ProjectResourceAssignment.objects.create(
        resource_type="case", object_id=case.pk, folder=current, assigned_by=request.user
    )


def case_library_url(case, user):
    url = reverse("ai_assistant:scenario_library") + f"?product={case.category.product_id}"
    assigned = ProjectResourceAssignment.objects.filter(
        resource_type="case", object_id=case.pk, folder__in=folders_for(user, case.category.product)
    ).first()
    return url + f"&folder={assigned.folder_id}" if assigned else url


def choose_product(request):
    if not can_create(request.user):
        raise PermissionDenied
    raw = request.GET.get("product", "")
    product = (
        get_object_or_404(Product, pk=int(raw))
        if raw.isascii() and raw.isdecimal() and len(raw) <= 18
        else None
    )
    if product is None:
        from django.http import Http404

        raise Http404
    return redirect(new_url(product))
