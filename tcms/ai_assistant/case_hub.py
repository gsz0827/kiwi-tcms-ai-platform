"""Product-scoped case hub with one directory pane for three case types."""

from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from tcms.management.models import Product
from tcms.web_testing.models import WebCase
from . import roles
from .case_directories import (
    COMMON,
    CASE_TYPES,
    can_manage_common,
    can_manage_folder,
    descendant_ids,
    filter_cases,
    folder_types,
    visible_folders,
)
from .case_library import visible_cases
from .models import APICase, ProjectResourceAssignment


def _url(name, params):
    return reverse(name) + "?" + urlencode(params)


def _row(kind, case, user):
    if kind == "manual":
        row = dict(
            number=f"TC-{case.pk}",
            name=case.summary,
            detail=f"{case.category.name} · {case.case_status.name}",
            product=case.category.product,
            text=case.text,
            resource_type="case",
            url=reverse("testcases-get", args=[case.pk]),
            can_move=user.has_perm("testcases.change_testcase")
            or user.has_perm("testcases.change_testcase", case),
        )
    elif kind == "web":
        row = dict(
            number=f"WEB-{case.pk}",
            name=case.name,
            detail="Web 页面操作与断言",
            product=case.product,
            text=case.description,
            resource_type="web_case",
            url=reverse("web_testing:case_edit", args=[case.pk]),
            can_move=True,
        )
    else:
        row = dict(
            number=f"API-{case.pk}",
            name=case.name,
            detail=f"{case.method} {case.path}",
            product=case.product,
            text="请求参数与断言请进入完整配置查看。",
            resource_type="api_case",
            url=reverse("ai_assistant:api_case_edit", args=[case.product_id, case.pk]),
            can_move=True,
        )
    row.update(kind=kind, pk=case.pk, modal_id=f"hub-case-{kind}-{case.pk}")
    row["can_move"] = row["can_move"] and not roles.is_read_only(user)
    return row


def _directories(request, product, selected, term, queries):
    from .templatetags.ai_navigation import _folder_tree

    kind_map = {"manual": "case", "web": "web_case", "api": "api_case"}
    types = CASE_TYPES if selected == "all" else folder_types(kind_map[selected])
    folders = list(
        visible_folders(request.user)
        .filter(resource_type__in=types)
        .select_related("product", "created_by")
        .filter(**({"product": product} if product else {}))
        .order_by("product__name", "position", "name", "pk")
    )
    counts = {}
    for kind, query in queries.items():
        if selected not in {"all", kind}:
            continue
        for count in (
            ProjectResourceAssignment.objects.filter(
                resource_type=kind_map[kind], object_id__in=query.values("pk"), folder__in=folders
            )
            .values("folder_id")
            .annotate(total=Count("pk"))
        ):
            counts[count["folder_id"]] = counts.get(count["folder_id"], 0) + count["total"]
    params = dict(product=product.pk if product else "", type=selected)
    if term:
        params["q"] = term
    nodes = _folder_tree(folders, {})
    descendants = {node.pk: descendant_ids(node, folders) for node in nodes}
    permissions = {}
    for node in nodes:
        node.case_count = sum(counts.get(pk, 0) for pk in descendants[node.pk])
        node.filter_url = _url("ai_assistant:case_hub", params | {"folder": node.pk})
        node.active = str(node.pk) == request.GET.get("folder", "")
        key = (node.product_id, node.resource_type)
        if key not in permissions:
            permissions[key] = can_manage_folder(request.user, node)
        node.can_manage = permissions[key]
        node.can_quick_manage = node.can_manage
        node.has_children = any(child.parent_id == node.pk for child in nodes)
        node.ancestor_ids = ",".join(
            str(parent.pk)
            for parent in nodes
            if node.pk in descendants[parent.pk] and parent.pk != node.pk
        )
        node.compatible_parents = [
            parent
            for parent in nodes
            if parent.product_id == node.product_id
            and parent.resource_type in folder_types(node.resource_type)
            and parent.pk not in descendants[node.pk]
        ]
    return dict(
        kind="case_hub",
        template="ai_assistant/_case_hub_directories.html",
        title="用例目录",
        can_quick_create=can_manage_common(request.user, product),
        quick_kind=COMMON,
        default_product_id=product.pk if product else None,
        folders=nodes,
        product=product,
        can_create=can_manage_common(request.user, product),
        can_manage=can_manage_common(request.user, product) or any(node.can_manage for node in nodes),
        common_parents=[node for node in nodes if node.resource_type == COMMON],
        all_url=_url("ai_assistant:case_hub", params),
        unfiled_url=_url("ai_assistant:case_hub", params | {"folder": "unfiled"}),
        selected_folder=request.GET.get("folder", ""),
    )


@login_required
def index(request):
    products = Product.objects.order_by("name")
    product_id = request.GET.get("product", request.session.get("ai_product_id", ""))
    product = get_object_or_404(products, pk=product_id) if str(product_id).isdigit() else None
    selected = request.GET.get("type", "all")
    if selected not in {"all", "manual", "web", "api"}:
        selected = "all"
    term = request.GET.get("q", "").strip()[:200]
    folder_id = request.GET.get("folder", "")
    queries = {
        "manual": visible_cases(request.user)
        .annotate(has_api=Exists(APICase.objects.filter(test_case_id=OuterRef("pk"))))
        .filter(is_automated=False, has_api=False)
        .select_related("category", "category__product", "case_status"),
        "web": WebCase.objects.filter(owner=request.user).select_related("product"),
        "api": APICase.objects.filter(owner=request.user).select_related("product"),
    }
    for kind, query in queries.items():
        if product:
            query = query.filter(**{"category__product" if kind == "manual" else "product": product})
        if term:
            query = query.filter(
                **{"summary__icontains" if kind == "manual" else "name__icontains": term}
            )
        queries[kind] = query
    browser = _directories(request, product, selected, term, queries)
    request.case_hub_browser = browser
    # All compatible folders are retained for row-level moves, even on a type-filtered tab.
    available = list(
        visible_folders(request.user).filter(resource_type__in=CASE_TYPES).select_related("product")
    )
    if product:
        available = [node for node in available if node.product_id == product.pk]
    from .templatetags.ai_navigation import _folder_tree

    available = _folder_tree(available, {})
    folder_map = {node.pk: node for node in available}
    for node in available:
        path, seen, current = [], set(), node
        while current and current.pk not in seen:
            seen.add(current.pk)
            path.append(current.name)
            current = folder_map.get(current.parent_id)
        node.path_label = " / ".join(reversed(path))
    sections, tabs, rows = [], [], {}
    page_size = (
        int(request.GET.get("page_size", "30"))
        if request.GET.get("page_size") in {"15", "30", "60"}
        else 30
    )
    base = {"product": product.pk if product else ""}
    if term:
        base["q"] = term
    if folder_id:
        base["folder"] = folder_id
    base["page_size"] = page_size
    tabs.append(
        dict(
            label="全部",
            url=_url("ai_assistant:case_hub", base),
            active=selected == "all",
            count=None,
        )
    )
    specs = (
        (
            "manual",
            "手工测试",
            "fa-hand-pointer-o",
            "case",
            "ai_assistant:case_library",
            {"type": "manual"},
        ),
        ("web", "Web 自动化测试", "fa-desktop", "web_case", "web_testing:cases", {}),
        (
            "api",
            "接口自动化测试",
            "fa-exchange",
            "api_case",
            "ai_assistant:api_home",
            {"tab": "cases"},
        ),
    )
    filtered = {}
    counts = {}
    for kind, _label, _icon, resource_type, _manage_name, _manage_params in specs:
        filtered[kind] = filter_cases(queries[kind], resource_type, folder_id, product).order_by(
            "-pk"
        )
        counts[kind] = filtered[kind].count()
    total = sum(counts.values()) if selected == "all" else counts[selected]
    # Pagination over lengths avoids loading/merging every private case into memory.
    page = Paginator(range(total), page_size).get_page(request.GET.get("page"))
    start, end, offset = page.start_index() - 1 if total else 0, page.end_index(), 0
    for kind, label, icon, resource_type, manage_name, manage_params in specs:
        query, count = filtered[kind], counts[kind]
        view_url = _url("ai_assistant:case_hub", base | {"type": kind})
        tabs.append(dict(label=label, url=view_url, active=selected == kind, count=count))
        if selected not in {"all", kind}:
            continue
        lower, upper = max(0, start - offset), min(count, end - offset)
        displayed = (
            [_row(kind, case, request.user) for case in query[lower:upper]] if upper > lower else []
        )
        offset += count
        for row in displayed:
            rows[(resource_type, row["pk"])] = row
        sections.append(
            dict(
                kind=kind,
                label=label,
                icon=icon,
                count=count,
                rows=displayed,
                page=page,
                view_url=view_url,
                has_more=False,
                manage_url=_url(manage_name, base | manage_params),
            )
        )
    assignments = {
        (item.resource_type, item.object_id): item.folder_id
        for item in ProjectResourceAssignment.objects.filter(
            resource_type__in=("case", "web_case", "api_case"),
            folder__in=available,
            object_id__in=[row["pk"] for row in rows.values()],
        )
    }
    for key, row in rows.items():
        row["available_folders"] = [
            node
            for node in available
            if node.product_id == row["product"].pk
            and node.resource_type in folder_types(row["resource_type"])
        ]
        row["folder_id"] = assignments.get(key)
        row["folder_name"] = next(
            (node.name for node in row["available_folders"] if node.pk == row["folder_id"]), "未归档"
        )
    browser["items"] = list(rows.values())
    return render(
        request,
        "ai_assistant/case_hub.html",
        dict(
            products=products,
            product=product,
            selected_type=selected,
            q=term,
            selected_folder=folder_id,
            sections=sections,
            tabs=tabs,
            directory_rows=list(rows.values()),
            directories=browser,
            page=page,
            page_size=page_size,
            page_sizes=(15, 30, 60),
            pagination_url=_url("ai_assistant:case_hub", base | {"type": selected}),
            batch_folders=[node for node in available if node.resource_type == COMMON],
            can_batch_move=not roles.is_read_only(request.user),
        ),
    )
