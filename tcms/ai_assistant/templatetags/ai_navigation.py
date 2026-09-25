from collections import defaultdict

from django import template

from tcms.ai_assistant.models import (
    AIRequest,
    ProjectResourceAssignment,
    ProjectResourceFolder,
)
from tcms.management.models import Product, Version
from tcms.testcases.models import TestCase
from tcms.testplans.models import TestPlan


register = template.Library()
RESOURCE_BROWSER_LIMIT = 20


def _with_resource_groups(items, group_label):
    for item in items:
        item.resource_group = group_label(item)
    return sorted(items, key=lambda item: (item.resource_group.casefold(), -item.pk))


def _resource_product_id(item, resource_type):
    if resource_type in {"requirement", "case"}:
        return item.category.product_id if item.category_id else None
    return item.product_id


def _folder_tree(folders, folder_items):
    children = defaultdict(list)
    folder_ids = {folder.pk for folder in folders}
    for folder in folders:
        parent_id = folder.parent_id if folder.parent_id in folder_ids else None
        children[parent_id].append(folder)
    for values in children.values():
        values.sort(key=lambda folder: (folder.position, folder.name.casefold(), folder.pk))

    nodes = []

    def append_children(parent_id, depth, ancestors):
        for folder in children.get(parent_id, ()):
            if folder.pk in ancestors:
                continue
            folder.resource_depth = depth
            folder.resource_indent = depth * 18
            folder.resource_items = folder_items.get(folder.pk, [])
            folder.option_prefix = "— " * depth
            nodes.append(folder)
            append_children(folder.pk, depth + 1, ancestors | {folder.pk})

    append_children(None, 0, set())
    return nodes


def _build_resource_browser(
    request, resource_type, title, icon, queryset, group_label
):
    total = queryset.count()
    items = list(queryset[:RESOURCE_BROWSER_LIMIT])
    items = _with_resource_groups(items, group_label)
    item_ids = [item.pk for item in items]

    folders = list(
        ProjectResourceFolder.objects.filter(resource_type=resource_type)
        .select_related("product", "parent", "created_by", "updated_by")
        .order_by("product__name", "position", "name", "pk")
    )
    assignments = {
        assignment.object_id: assignment.folder_id
        for assignment in ProjectResourceAssignment.objects.filter(
            resource_type=resource_type, object_id__in=item_ids
        )
    }
    folders_by_product = defaultdict(list)
    for folder in folders:
        folders_by_product[folder.product_id].append(folder)

    folder_items = defaultdict(list)
    unfiled_items = []
    for item in items:
        product_id = _resource_product_id(item, resource_type)
        item.resource_folder_id = assignments.get(item.pk)
        item.available_folders = folders_by_product.get(product_id, [])
        if item.resource_folder_id:
            folder_items[item.resource_folder_id].append(item)
        else:
            unfiled_items.append(item)

    folder_nodes = _folder_tree(folders, folder_items)
    can_manage = resource_type == "requirement" or request.user.has_perm(
        {
            "case": "testcases.change_testcase",
            "plan": "testplans.change_testplan",
        }[resource_type]
    )
    return {
        "kind": resource_type,
        "title": title,
        "icon": icon,
        "items": items,
        "unfiled_items": unfiled_items,
        "folder_nodes": folder_nodes,
        "folders": folders,
        "products": Product.objects.order_by("name"),
        "can_manage": can_manage,
        "total": total,
        "has_more": total > RESOURCE_BROWSER_LIMIT,
    }


@register.simple_tag(takes_context=True)
def platform_resource_browser(context):
    request = context.get("request")
    # 与 ai_project_switcher 同理：错误页面可能在认证中间件之前渲染，那时
    # request 上没有 user。这个标签在 base.html 里被无条件调用，少了这层
    # 判断，500 页面自身就会崩。
    user = getattr(request, "user", None)
    if request is None or user is None or not user.is_authenticated:
        return None

    resolver_match = getattr(request, "resolver_match", None)
    current = getattr(resolver_match, "url_name", "")

    if current == "index" and getattr(resolver_match, "app_name", "") == "ai_assistant":
        queryset = AIRequest.objects.filter(created_by=request.user).select_related(
            "category", "category__product"
        ).order_by("-created")
        return _build_resource_browser(
            request,
            "requirement",
            "需求目录",
            "fa-lightbulb-o",
            queryset,
            lambda item: (
                f"{item.category.product.name} / {item.category.name}"
                if item.category_id
                else "未分类需求"
            ),
        )

    if current == "testcases-search":
        queryset = TestCase.objects.select_related(
            "case_status",
            "category",
            "category__product",
            "priority",
            "author",
            "default_tester",
        )
        product_id = request.GET.get("product")
        if product_id and product_id.isdigit():
            queryset = queryset.filter(category__product_id=product_id)
        return _build_resource_browser(
            request,
            "case",
            "用例目录",
            "fa-list",
            queryset.order_by("-pk"),
            lambda item: f"{item.category.product.name} / {item.category.name}",
        )

    if current == "plans-search":
        queryset = TestPlan.objects.select_related(
            "product", "product_version", "type", "author"
        ).prefetch_related("cases")
        product_id = request.GET.get("product")
        if product_id and product_id.isdigit():
            queryset = queryset.filter(product_id=product_id)
        return _build_resource_browser(
            request,
            "plan",
            "计划目录",
            "fa-map-o",
            queryset.order_by("-pk"),
            lambda item: f"{item.product.name} / {item.product_version.value}",
        )

    return None


@register.inclusion_tag(
    "ai_assistant/_project_switcher.html", takes_context=True
)
def ai_project_switcher(context):
    request = context.get("request")
    resolver_match = getattr(request, "resolver_match", None)
    user = getattr(request, "user", None)
    session = getattr(request, "session", None)
    # request 存在但缺少 user/session 是真实可能发生的：错误页面会在中间件链
    # 提前失败时渲染，那时认证与会话中间件都还没执行。这里必须容错，否则 500
    # 页面自身会崩溃，用户只能拿到一个空白响应，连请求编号都看不到。
    if (
        request is None
        or user is None
        or not user.is_authenticated
        or session is None
        or getattr(resolver_match, "app_name", "") != "ai_assistant"
    ):
        return {"show_switcher": False}

    product_id = session.get("ai_product_id")
    version_id = session.get("ai_version_id")
    products = Product.objects.order_by("name")
    versions = Version.objects.select_related("product").order_by(
        "product__name", "value"
    )
    selected_product = products.filter(pk=product_id).first() if product_id else None
    selected_version = versions.filter(pk=version_id).first() if version_id else None
    return {
        "show_switcher": True,
        "request": request,
        "csrf_token": context.get("csrf_token"),
        "products": products,
        "versions": versions,
        "selected_product": selected_product,
        "selected_version": selected_version,
    }
