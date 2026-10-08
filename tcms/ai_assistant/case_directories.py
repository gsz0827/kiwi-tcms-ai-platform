"""Shared business folders alongside legacy type-specific folders, without data rewrites."""
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib import messages

from tcms.management.models import Product
from tcms.web_testing.models import WebCase
from . import roles
from .case_library import visible_cases
from .models import APICase, ProjectResourceAssignment, ProjectResourceFolder

COMMON = "case_group"
CASE_TYPES = {"case", "web_case", "api_case", COMMON}


def folder_types(kind):
    return {kind, COMMON} if kind in CASE_TYPES else {kind}


def visible_folders(user):
    query = ProjectResourceFolder.objects.all()
    if user.is_superuser or user.has_perm("testcases.view_testcase") or user.has_perm("testcases.change_testcase"):
        return query.filter(~Q(resource_type="requirement") |
                            Q(product__in=roles.member_products(user)))
    products = Product.objects.filter(Q(pk__in=roles.member_products(user)) |
        Q(pk__in=WebCase.objects.filter(owner=user).values("product_id")) |
        Q(pk__in=APICase.objects.filter(owner=user).values("product_id")) |
        Q(pk__in=visible_cases(user).values("category__product_id")))
    return query.filter(~Q(resource_type=COMMON) | Q(product__in=products)).filter(
        ~Q(resource_type="requirement") | Q(product__in=roles.member_products(user)))


def can_manage_common(user, product):
    if not user.is_authenticated or not user.is_active or roles.is_read_only(user) or product is None:
        return False
    return bool(user.is_superuser or user.has_perm("testcases.change_testcase") or
        roles.is_product_member(user, product) or
        WebCase.objects.filter(owner=user, product=product).exists() or
        APICase.objects.filter(owner=user, product=product).exists() or
        visible_cases(user).filter(category__product=product, author=user).exists())


def can_manage_folder(user, folder):
    if not user.is_authenticated or not user.is_active or roles.is_read_only(user):
        return False
    if folder.resource_type == "requirement":
        return roles.can_manage_requirement_directories(user, folder.product)
    if folder.resource_type == COMMON:
        return can_manage_common(user, folder.product)
    if folder.resource_type == "case":
        return user.has_perm("testcases.change_testcase")
    if folder.resource_type in {"plan", "run"}:
        permission = "testplans.change_testplan" if folder.resource_type == "plan" else "testruns.change_testrun"
        return user.has_perm(permission)
    return folder.resource_type in {"web_case", "api_case"} and user.is_active


def descendant_ids(folder, folders):
    ids = {folder.pk}
    while True:
        expanded = ids | {node.pk for node in folders if node.parent_id in ids}
        if expanded == ids:
            return ids
        ids = expanded


def filter_cases(query, kind, selected, product=None, *, product_field=None):
    if not selected:
        return query
    product_field = product_field or ("category__product_id" if kind == "case" else "product_id")
    assignments = ProjectResourceAssignment.objects.filter(resource_type=kind,
        folder__resource_type__in=folder_types(kind), folder__product_id=OuterRef(product_field))
    if selected == "unfiled":
        return query.annotate(_directory_assigned=Exists(assignments.filter(object_id=OuterRef("pk")))).filter(_directory_assigned=False)
    folders = ProjectResourceFolder.objects.filter(resource_type__in=folder_types(kind))
    if product is not None:
        folders = folders.filter(product=product)
    folder = folders.filter(pk=selected).first() if str(selected).isdigit() else None
    if not folder:
        return query.none()
    ids = descendant_ids(folder, list(folders.filter(product_id=folder.product_id)))
    assignments = assignments.filter(folder_id__in=ids, object_id=OuterRef("pk"))
    return query.annotate(_directory_selected=Exists(assignments)).filter(_directory_selected=True)


@login_required
@require_POST
def move_folder(request, pk):
    from .views import _resource_browser_redirect
    folder = get_object_or_404(ProjectResourceFolder.objects.select_related("product"), pk=pk)
    if not can_manage_folder(request.user, folder):
        raise PermissionDenied
    try:
        with transaction.atomic():
            Product.objects.select_for_update().get(pk=folder.product_id)
            nodes = list(ProjectResourceFolder.objects.select_for_update().filter(product_id=folder.product_id))
            folder = next(node for node in nodes if node.pk == pk)
            parent_id = request.POST.get("parent", "")
            parent = next((node for node in nodes if str(node.pk) == parent_id), None) if parent_id else None
            if parent_id and (parent is None or parent.resource_type not in folder_types(folder.resource_type)):
                raise PermissionDenied
            if parent and parent.pk in descendant_ids(folder, nodes):
                raise ValueError("不能将目录移到自己或自己的子目录下。")
            if ProjectResourceFolder.objects.filter(product_id=folder.product_id, resource_type=folder.resource_type,
                parent=parent, name__iexact=folder.name).exclude(pk=folder.pk).exists():
                raise ValueError("目标目录下已有同名目录，请先重命名。")
            folder.parent, folder.updated_by = parent, request.user
            folder.save(update_fields=("parent", "updated_by", "updated"))
    except (ValueError, IntegrityError) as exc:
        messages.error(request, str(exc) if isinstance(exc, ValueError) else "目录已被修改，请刷新后重试。")
    else:
        messages.success(request, "目录位置已更新，原有资源与记录保持不变。")
    return _resource_browser_redirect(request, folder.resource_type)
