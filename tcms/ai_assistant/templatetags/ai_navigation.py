from collections import defaultdict

from django import template
from django.conf import settings
from django.urls import NoReverseMatch, reverse

from tcms.ai_assistant import case_library, roles
from tcms.ai_assistant.models import (
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
    request, resource_type, title, icon, queryset, group_label, folder_product=None,
    limit=RESOURCE_BROWSER_LIMIT,
):
    """右侧页面的共享目录面板。

    folder_product 给定时只列该项目的目录（AI 用例库是单项目视图，310px
    的窄栏里混进别的项目的目录只会更乱），并在返回值里给出 default_product_id
    供「新建目录」弹窗预选。
    """
    total = queryset.count()
    items = list(queryset[:limit])
    items = _with_resource_groups(items, group_label)
    item_ids = [item.pk for item in items]

    from tcms.ai_assistant.case_directories import folder_types, visible_folders
    folder_query = visible_folders(request.user).filter(resource_type__in=folder_types(resource_type))
    if folder_product is not None:
        folder_query = folder_query.filter(product=folder_product)
    folders = list(
        folder_query.select_related("product", "parent", "created_by", "updated_by")
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
    if resource_type in {"case", "web_case", "api_case"}:
        from tcms.ai_assistant.case_directories import can_manage_folder
        managed_folder_nodes = [node for node in folder_nodes if can_manage_folder(request.user, node)]
    else:
        managed_folder_nodes = folder_nodes
    can_manage = (resource_type in {"web_case", "api_case"} and not roles.is_read_only(request.user)) or resource_type == "requirement" or (resource_type not in {"web_case", "api_case"} and request.user.has_perm(
        {
            "case": "testcases.change_testcase",
            "plan": "testplans.change_testplan",
        }[resource_type]
    ))
    quick_manage = request.user.is_active and not roles.is_read_only(request.user)
    case_kinds = {"case", "web_case", "api_case"}
    from tcms.ai_assistant.case_directories import can_manage_common
    quick_create = quick_manage and (
        can_manage_common(request.user, folder_product)
        if resource_type in case_kinds and folder_product is not None else can_manage
    )
    managed_ids = {node.pk for node in managed_folder_nodes}
    for node in folder_nodes:
        node.can_quick_manage = quick_manage and node.pk in managed_ids and (
            resource_type in case_kinds or can_manage
        )
    for item in items:
        item.tree_kind = {"case": "manual", "web_case": "web", "api_case": "api"}.get(resource_type, "")
        item.tree_product_id = _resource_product_id(item, resource_type)
        item.tree_name = item.summary if resource_type == "case" else getattr(item, "name", "")
        item.can_tree_rename = bool(item.tree_kind) and request.user.is_active and not roles.is_read_only(request.user)
        if resource_type == "case":
            item.can_tree_rename = item.can_tree_rename and (
                request.user.has_perm("testcases.view_testcase") or request.user.has_perm("testcases.view_testcase", item)
            ) and (
                request.user.has_perm("testcases.change_testcase") or request.user.has_perm("testcases.change_testcase", item)
            )
    return {
        "kind": resource_type,
        "can_quick_create": quick_create,
        "quick_kind": "case_group" if resource_type in {"case", "web_case", "api_case"} else resource_type,
        "title": title,
        "icon": icon,
        "items": items,
        "unfiled_items": unfiled_items,
        "folder_nodes": folder_nodes,
        "managed_folder_nodes": managed_folder_nodes,
        "folders": folders,
        "products": Product.objects.order_by("name"),
        "can_manage": can_manage,
        "default_product_id": folder_product.pk if folder_product is not None else None,
        "total": total,
        "has_more": total > limit,
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

    app_name = getattr(resolver_match, "app_name", "")
    if app_name == "ai_assistant" and current in {"case_hub", "scenario_library"}:
        return getattr(request, "case_hub_browser", None)
    is_web_cases = app_name == "web_testing" and current == "cases"
    is_api_cases = app_name == "ai_assistant" and current == "api_home" and request.GET.get("tab") == "cases"
    if is_web_cases or is_api_cases:
        from tcms.ai_assistant.automation_folders import automation_browser
        _products, product = case_library.selected_product(request)
        if product is not None:
            return automation_browser(request, "web_case" if is_web_cases else "api_case", product, context.get("cases"))
        return None

    if current == "index" and getattr(resolver_match, "app_name", "") == "ai_assistant":
        # 与需求列表一致：本项目成员互相可见，不再只看自己提的需求。
        queryset = roles.visible_requests(request.user).select_related(
            "category", "category__product"
        ).order_by("-created")
        product_id = str(request.GET.get("product", request.session.get("ai_product_id", "")))
        if product_id.isdigit():
            queryset = queryset.filter(category__product_id=product_id)
        from tcms.ai_assistant.requirement_directory import requirement_browser
        return requirement_browser(request, queryset, product_id)

    if current == "case_library" and getattr(resolver_match, "app_name", "") == "ai_assistant":
        # 用例库是单项目视图：目录栏跟随页面筛选，项目目录只列当前项目。
        _products, product = case_library.selected_product(request)
        queryset = case_library.library_cases(request, product).select_related(
            "case_status",
            "category",
            "category__product",
            "priority",
            "author",
            "default_tester",
        )
        return _build_resource_browser(
            request,
            "case",
            "用例目录",
            "fa-list",
            queryset.order_by("-pk"),
            lambda item: f"{item.category.product.name} / {item.category.name}",
            folder_product=product,
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

    if current == "testruns-search" and hasattr(request, "run_browser"):
        return request.run_browser

    if current == "plans-search":
        if hasattr(request, "plan_browser"):
            return request.plan_browser
        queryset = TestPlan.objects.select_related(
            "product", "product_version", "type", "author"
        )
        from django.db.models import Count
        queryset = queryset.annotate(case_total=Count("cases", distinct=True))
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
    ):
        return {"show_switcher": False}

    product_id = request.GET.get("product", session.get("ai_product_id"))
    version_id = request.GET.get("version", session.get("ai_version_id"))
    product_id = int(product_id) if str(product_id).isascii() and str(product_id).isdigit() and len(str(product_id)) <= 18 else None
    version_id = int(version_id) if str(version_id).isascii() and str(version_id).isdigit() and len(str(version_id)) <= 18 else None
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


# ---------------------------------------------------------------------------
# 中文侧边栏的结构与选中态
#
# 结构以前写在模板里，同一个 if 被复制三遍（is-current / aria-expanded /
# collapse 的 in），判据是 '/case/' in path 这类子串匹配。新增页面必然漏掉其中
# 一处，于是"展开了 A 却高亮 B"成了常态。现在结构与判定都在这里定义，模板只负责
# 循环渲染。
#
# fragments 是（子串, 排除子串）二元组：前者命中且后者不命中才算匹配，用来表达
# 「/cases/ 属于用例库，但 /ai/api-testing/.../cases/new/ 不算」。
# ---------------------------------------------------------------------------

from tcms.web_testing.navigation import TEST_NAV_SECTIONS

NAV_SECTIONS = TEST_NAV_SECTIONS + (
    {
        "key": "quality",
        "label": "质量与缺陷",
        "icon": "fa-shield",
        "items": (
            {
                "label": "质量看板",
                "icon": "fa-dashboard",
                "url": "ai_assistant:dashboard",
                "names": ("ai_assistant:dashboard",),
            },
            {
                "label": "测试报告",
                "icon": "fa-files-o",
                "url": "ai_assistant:iteration_reports",
                "names": (
                    "ai_assistant:iteration_reports",
                    "ai_assistant:iteration_report_detail",
                    "ai_assistant:run_report",
                    "ai_assistant:edit_report",
                    "ai_assistant:approve_report",
                    "ai_assistant:evaluate_report_gate",
                    "ai_assistant:export_report_html",
                    "ai_assistant:export_report_pdf",
                    "ai_assistant:create_regression_verification",
                ),
            },
            {
                "label": "质量趋势",
                "icon": "fa-line-chart",
                "url": "ai_assistant:report_trends",
                "names": ("ai_assistant:report_trends",),
            },
            {
                "label": "缺陷列表",
                "icon": "fa-bug",
                "url": "bugs-search",
                "fragments": (("/bugs/", None),),
            },
            {
                "label": "缺陷与复测",
                "icon": "fa-refresh",
                "url": "ai_assistant:dashboard",
                "anchor": "defect-management",
                "names": (
                    "ai_assistant:execution_defect",
                    "ai_assistant:edit_defect_draft",
                    "ai_assistant:link_defect_draft",
                    "ai_assistant:sync_defect_status",
                    "ai_assistant:create_defect_regression",
                ),
            },
            {
                "label": "门禁设置",
                "icon": "fa-lock",
                "url": "ai_assistant:release_gate_settings",
                "names": ("ai_assistant:release_gate_settings",),
                "perm": "ai_assistant.approve_aireport",
            },
        ),
    },
    {
        "key": "metrics",
        "label": "度量分析",
        "icon": "fa-bar-chart",
        "items": (
            {
                "label": "测试分布",
                "icon": "fa-pie-chart",
                "url": "testing-breakdown",
                "names": ("testing-breakdown",),
            },
            {
                "label": "执行总览",
                "icon": "fa-tachometer",
                "url": "execution-dashboard",
                "names": ("execution-dashboard",),
            },
            {
                "label": "状态矩阵",
                "icon": "fa-th",
                "url": "testing-status-matrix",
                "names": ("testing-status-matrix",),
            },
            {
                "label": "执行趋势",
                "icon": "fa-line-chart",
                "url": "testing-execution-trends",
                "names": ("testing-execution-trends",),
            },
            {
                "label": "用例健康度",
                "icon": "fa-heartbeat",
                "url": "test-case-health",
                "names": ("test-case-health",),
            },
        ),
    },
    {
        "key": "platform",
        "label": "平台管理",
        "icon": "fa-cog",
        "items": (
            {
                "label": "项目配置",
                "icon": "fa-cog",
                "url": "ai_assistant:project_settings",
                "names": (
                    "ai_assistant:project_settings",
                    "ai_assistant:create_product",
                ),
            },
            {
                "label": "AI 模型配置",
                "icon": "fa-key",
                "url": "ai_assistant:model_settings",
                "names": (
                    "ai_assistant:model_settings",
                    "ai_assistant:edit_model_config",
                    "ai_assistant:activate_model_config",
                    "ai_assistant:test_model_config",
                ),
            },
            {
                "label": "AI 调用记录",
                "icon": "fa-list-alt",
                "url": "ai_assistant:usage_logs",
                "names": ("ai_assistant:usage_logs",),
            },
            {
                "label": "AI 规则包",
                "icon": "fa-file-text-o",
                "url": "ai_assistant:instruction_profiles",
                "names": (
                    "ai_assistant:instruction_profiles",
                    "ai_assistant:edit_instruction_profile",
                    "ai_assistant:toggle_instruction_profile",
                ),
            },
            {
                "label": "成员与角色",
                "icon": "fa-users",
                "url": "ai_assistant:member_list",
                "perm": "ai_assistant.manage_members",
                "names": (
                    "ai_assistant:member_list",
                    "ai_assistant:add_product_member",
                    "ai_assistant:remove_product_member",
                    "ai_assistant:set_member_roles",
                ),
            },
        ),
    },
)


def _item_is_active(item, current, path):
    if current and current in item.get("names", ()):
        return True
    for fragment, exclude in item.get("fragments", ()):
        if fragment in path and (exclude is None or exclude not in path):
            return True
    return False


def _user_has_perm(request, permission):
    """判断当前用户是否有某个能力；匿名请求一律没有。"""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return user.has_perm(permission) or user.is_staff


@register.simple_tag(takes_context=True)
def platform_navigation(context):
    """侧边栏的分区结构、当前分区与当前页，供模板直接循环渲染。"""
    request = context.get("request")
    resolver_match = getattr(request, "resolver_match", None)
    url_name = getattr(resolver_match, "url_name", "") or ""
    app_name = getattr(resolver_match, "app_name", "") or ""
    current = f"{app_name}:{url_name}" if app_name else url_name
    if app_name == "allure_reporting":
        kind = getattr(resolver_match, "kwargs", {}).get("kind")
        app_name, current = ("web_testing", "web_testing:run") if kind == "web" else ("ai_assistant", "ai_assistant:api_report")
    if current == "ai_assistant:automation_archive":
        kind = getattr(resolver_match, "kwargs", {}).get("kind")
        if kind == "web":
            app_name, current = "web_testing", "web_testing:run"
        elif kind == "api":
            current = "ai_assistant:api_report"
    path = getattr(request, "path", "") or ""

    plugin_active = False
    sections = []
    for section in NAV_SECTIONS:
        items = []
        section_active = False
        for item in section["items"]:
            try:
                url = reverse(item["url"])
            except NoReverseMatch:  # 名称写错不应该让整个页面渲染失败
                continue
            permission = item.get("perm")
            if permission and not _user_has_perm(request, permission):
                # 没这个能力的人不该看见入口：点进去只会拿到 403。
                continue
            is_active = _item_is_active(item, current, path)
            if any(current in candidate.get("names", ()) for group in NAV_SECTIONS for candidate in group["items"]):
                is_active = current in item.get("names", ())
            if app_name == "web_testing" and section["key"] != "web":
                is_active = False
            if section["key"] == "api" and current == "ai_assistant:api_home":
                is_active = item.get("query") == "tab=" + request.GET.get("tab", "environments")
            query = item.get("query")
            if query:
                url += "?" + query
            section_active = section_active or is_active
            anchor = item.get("anchor")
            items.append(
                {
                    "kind": "leaf",
                    "label": item["label"],
                    "icon": item["icon"],
                    "url": f"{url}#{anchor}" if anchor else url,
                    "is_active": is_active,
                }
            )

        if section["key"] == "platform":
            plugins = plugin_menu_entries()
            if plugins:
                items.append({"kind": "caption", "label": "插件"})
                for entry in plugins:
                    if entry["kind"] == "caption":
                        items.append(entry)
                        continue
                    leaf_active = entry["url"] == path
                    plugin_active = plugin_active or leaf_active
                    items.append(
                        {
                            "kind": "leaf",
                            "label": entry["label"],
                            "icon": "fa-puzzle-piece",
                            "url": entry["url"],
                            "is_active": leaf_active,
                        }
                    )
            section_active = section_active or plugin_active

        sections.append(
            {
                "key": section["key"],
                "label": section["label"],
                "icon": section["icon"],
                "is_current": section_active,
                "items": items,
            }
        )

    return {
        "home_active": current == "core-views-index",
        "sections": sections,
    }


# ---------------------------------------------------------------------------
# 插件菜单
#
# 上游约定 SETTINGS.MENU_ITEMS 的最后一项固定留给插件（kiwitcms.plugins 的
# entry point 用 {plugin}.menu.MENU_ITEMS 往这一项里追加）。本分支的侧边栏改成
# 显式声明（NAV_SECTIONS）之后，上游菜单不再投影到界面上 —— 其中真正有价值的
# 入口已经逐个收编进上面的分区，重复的快捷方式则被丢弃。但插件注册的入口无法
# 预知，只能运行时收集，否则装上插件等于没有入口。
#
# 没有安装插件时这里返回空列表，界面上不会出现任何多余分组。
#
# 维护提示：升级上游版本后请比对 tcms/settings/common.py 的 MENU_ITEMS，确认
# 新增的入口已经在本文件的 NAV_SECTIONS 里找到位置。
# ---------------------------------------------------------------------------


def _collect_menu_entries(items):
    """把插件注册的菜单整理成 (标题, 链接或子菜单) 列表。"""
    entries = []
    for entry in items:
        # MENU_ITEMS 里有些条目在可选应用未安装时是空元组，必须先挡住
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        label, target = entry
        if label == "-":
            continue
        if isinstance(target, list):
            children = _collect_menu_entries(target)
            if children:
                entries.append((label, children))
            continue
        entries.append((label, str(target)))
    return entries


def _flatten_leaves(entries):
    """把任意深度的插件菜单条目拍成一串叶子，侧边栏里不再递归缩进。"""
    leaves = []
    for label, target in entries:
        if isinstance(target, list):
            leaves.extend(_flatten_leaves(target))
        else:
            leaves.append((label, target))
    return leaves


def plugin_menu_entries():
    """插件注册的菜单条目。

    顶层分组（例如插件自己分出来的「子菜单」）渲染成 `kiwi-nav-caption` 小标题，
    更深的层级一律拍平成叶子 —— 侧边栏只有 232px 宽，再缩进就没法看了。没有装
    插件时返回空列表，界面上不会出现任何多余分组。
    """
    raw_groups = list(settings.MENU_ITEMS)
    if not raw_groups:
        return []
    # tcms/settings/common.py 的约定：最后一项固定是留给插件扩展的 MORE
    _label, targets = raw_groups[-1]
    if not isinstance(targets, list):
        return []

    items = []
    for label, target in _collect_menu_entries(targets):
        if not isinstance(target, list):
            items.append({"kind": "leaf", "label": label, "url": target})
            continue
        leaves = _flatten_leaves(target)
        if not leaves:
            continue
        items.append({"kind": "caption", "label": label})
        items.extend(
            {"kind": "leaf", "label": leaf_label, "url": leaf_url}
            for leaf_label, leaf_url in leaves
        )
    return items
