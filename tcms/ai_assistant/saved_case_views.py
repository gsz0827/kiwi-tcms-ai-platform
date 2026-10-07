"""Owner-only case filter bookmarks; loading never grants access to saved resources."""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from tcms.management.models import Product
from .case_directories import CASE_TYPES, folder_types, visible_folders
from .models import SavedCaseView
from .project_context import return_url

FIELDS = ("product", "type", "folder", "q", "page_size")
LIMIT = 50
TYPE_LABELS = {"all": "全部类型", "manual": "手工测试", "web": "Web 自动化", "api": "接口自动化"}


def _identifier(value):
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and 0 < len(value) <= 18
        and int(value) > 0
    )


def normalize(user, data, *, lookup=None, stored=False):
    if not isinstance(data, dict) or (stored and set(data) - set(FIELDS)):
        raise ValueError("视图条件格式不正确，请使用当前筛选重新保存。")
    values = {key: data.get(key, "") for key in FIELDS}
    if any(not isinstance(value, str) for value in values.values()):
        raise ValueError("视图条件格式不正确，请使用当前筛选重新保存。")
    values["type"] = values["type"] or "all"
    values["page_size"] = values["page_size"] or "30"
    if values["type"] not in TYPE_LABELS or values["page_size"] not in {"15", "30", "60"}:
        raise ValueError("用例类型或每页条数无效。")
    values["q"] = values["q"].strip()
    if len(values["q"]) > 200:
        raise ValueError("搜索内容不能超过 200 个字符。")
    product, folder = None, None
    if values["product"]:
        if not _identifier(values["product"]):
            raise ValueError("项目筛选无效，请重新选择。")
        product = (
            lookup[0].get(int(values["product"]))
            if lookup is not None
            else Product.objects.filter(pk=int(values["product"])).first()
        )
        if product is None:
            raise ValueError("保存的项目已不存在，请选择有效项目后更新视图。")
        values["product"] = str(product.pk)
    if values["folder"] not in {"", "unfiled"}:
        if not _identifier(values["folder"]):
            raise ValueError("目录筛选无效，请重新选择。")
        folder = (
            lookup[1].get(int(values["folder"]))
            if lookup is not None
            else visible_folders(user)
            .filter(pk=int(values["folder"]))
            .select_related("product")
            .first()
        )
        if folder is None or folder.resource_type not in CASE_TYPES:
            raise ValueError("保存的目录已删除或不再可见，请重新选择目录后更新视图。")
        if product and folder.product_id != product.pk:
            raise ValueError("目录不属于所选项目，请重新选择。")
        raw_kind = {"manual": "case", "web": "web_case", "api": "api_case"}.get(values["type"])
        if raw_kind and folder.resource_type not in folder_types(raw_kind):
            raise ValueError("目录与当前用例类型不兼容，请重新选择。")
        values["folder"] = str(folder.pk)
    return values, product, folder


def hub_url(filters):
    return reverse("ai_assistant:case_hub") + "?" + urlencode(filters)


def context(user, current):
    views = list(SavedCaseView.objects.filter(owner=user)[:LIMIT])
    # Resolve saved references in two bounded queries, not one query per bookmark.
    product_ids, folder_ids = set(), set()
    for item in views:
        if isinstance(item.filters, dict):
            for key, ids in (("product", product_ids), ("folder", folder_ids)):
                value = item.filters.get(key)
                if _identifier(value):
                    ids.add(int(value))
    lookup = (
        {item.pk: item for item in Product.objects.filter(pk__in=product_ids)},
        {
            item.pk: item
            for item in visible_folders(user).filter(pk__in=folder_ids).select_related("product")
        },
    )
    for item in views:
        try:
            values, product, folder = normalize(user, item.filters, lookup=lookup, stored=True)
            item.filter_description = " · ".join(
                (
                    product.name if product else "全部项目",
                    TYPE_LABELS[values["type"]],
                    (
                        folder.name
                        if folder
                        else ("未归档" if values["folder"] == "unfiled" else "全部目录")
                    ),
                )
            )
            if values["q"]:
                item.filter_description += " · 搜索：" + values["q"]
            item.invalid_reason = ""
            item.matches_current = values == current
        except ValueError as error:
            item.filter_description, item.invalid_reason = "条件需要更新", str(error)
            item.matches_current = False
    try:
        normalize(user, current)
        current_error = ""
    except ValueError as error:
        current_error = str(error)
    return dict(
        saved_case_views=views,
        current_case_filters=current,
        current_case_filter_error=current_error,
        saved_case_view_limit=LIMIT,
    )


def _name(request):
    name = request.POST.get("name", "").strip()
    if not name or len(name) > 80:
        raise ValueError("视图名称不能为空且不能超过 80 个字符。")
    return name


def _check_revision(request, item):
    revision = request.POST.get("revision", "")
    if not _identifier(revision) or int(revision) != item.revision:
        raise ValueError("视图已在其他页面更新，请刷新后重试；本次没有覆盖原有条件。")


@login_required
@require_POST
def save(request, pk=None):
    if not request.user.is_active:
        raise PermissionDenied
    action = request.POST.get("action", "create" if pk is None else "rename")
    if action not in ({"create"} if pk is None else {"rename", "replace"}):
        raise PermissionDenied
    try:
        name = _name(request)
        values = normalize(request.user, request.POST)[0] if action != "rename" else None
        with transaction.atomic():
            # Serialize quota/name/revision changes for one account, including concurrent tabs.
            get_user_model().objects.select_for_update().get(pk=request.user.pk)
            item = (
                get_object_or_404(
                    SavedCaseView.objects.select_for_update(), owner=request.user, pk=pk
                )
                if pk is not None
                else None
            )
            if item:
                _check_revision(request, item)
            elif SavedCaseView.objects.filter(owner=request.user).count() >= LIMIT:
                raise ValueError(f"最多保存 {LIMIT} 个个人视图，请先删除不再使用的视图。")
            if SavedCaseView.objects.filter(owner=request.user, name=name).exclude(pk=pk).exists():
                raise ValueError("已有同名视图。请换个名称，或在管理窗口明确更新原视图。")
            if item:
                item.name = name
                item.revision += 1
                if values is not None:
                    item.filters = values
                item.save(update_fields=("name", "filters", "revision", "updated"))
            else:
                SavedCaseView.objects.create(owner=request.user, name=name, filters=values)
        messages.success(request, "个人筛选视图已保存；只保存条件，不保存或执行用例。")
    except (ValueError, IntegrityError) as error:
        messages.error(
            request, str(error) if isinstance(error, ValueError) else "已有同名视图，请刷新后重试。"
        )
    return HttpResponseRedirect(return_url(request))


@login_required
@require_GET
def load(request, pk):
    item = get_object_or_404(SavedCaseView, owner=request.user, pk=pk)
    try:
        values, _product, _folder = normalize(request.user, item.filters, stored=True)
    except ValueError as error:
        # A stale folder must never silently become 'all cases'.
        return render(
            request,
            "ai_assistant/saved_case_view_unavailable.html",
            {"saved_case_view": item, "reason": str(error)},
            status=409,
        )
    return HttpResponseRedirect(hub_url(values))


@login_required
@require_POST
def delete(request, pk):
    if not request.user.is_active:
        raise PermissionDenied
    try:
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=request.user.pk)
            item = get_object_or_404(
                SavedCaseView.objects.select_for_update(), owner=request.user, pk=pk
            )
            _check_revision(request, item)
            item.delete()
        messages.success(request, "个人视图已删除；项目目录、用例和执行结果未受影响。")
    except ValueError as error:
        messages.error(request, str(error))
    return HttpResponseRedirect(return_url(request))
