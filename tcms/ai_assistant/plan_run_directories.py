"""Product-rooted navigation for plans and native execution tasks."""

from collections import defaultdict

from django.urls import reverse
from guardian.shortcuts import get_objects_for_user

from tcms.management.models import Build, Product, Version
from tcms.testruns.models import TestRun
from . import roles
from .case_directories import can_manage_folder, visible_folders
from .models import ProjectResourceAssignment
from .product_case_tree import folder_paths

TREE_LIMIT = 2000


def build_tree(request, kind, query, data):
    is_plan = kind == "plan"
    permission = "testplans.change_testplan" if is_plan else "testruns.change_testrun"
    title, prefix = ("计划目录", "TP") if is_plan else ("执行目录", "TR")
    search_url = reverse("plans-search" if is_plan else "testruns-search")
    products = list(Product.objects.order_by("name", "pk"))
    folders = list(
        visible_folders(request.user)
        .filter(resource_type=kind)
        .select_related("product")
        .order_by("position", "name", "pk")
    )
    by_id = {folder.pk: folder for folder in folders}
    paths = folder_paths(folders)
    items = list(query[: TREE_LIMIT + 1])
    has_more = len(items) > TREE_LIMIT
    items = items[:TREE_LIMIT]
    assignments = dict(
        ProjectResourceAssignment.objects.filter(
            resource_type=kind, object_id__in=[item.pk for item in items], folder_id__in=by_id
        ).values_list("object_id", "folder_id")
    )
    hierarchy = None
    if is_plan:
        from tcms.testplans.plan_hierarchy import hierarchy_for

        hierarchy = hierarchy_for(request)
        assignments = hierarchy.folders(folders)
    item_ids = {item.pk for item in items}
    plan_children = defaultdict(list)
    editable = set()
    if not roles.is_read_only(request.user):
        editable = set(
            get_objects_for_user(request.user, permission, klass=query.model)
            .filter(pk__in=[item.pk for item in items])
            .values_list("pk", flat=True)
        )
    children, grouped = defaultdict(list), defaultdict(list)
    for folder in folders:
        parent = by_id.get(folder.parent_id)
        parent_id = parent.pk if parent and parent.product_id == folder.product_id else None
        children[(folder.product_id, parent_id)].append(folder)
    for item in items:
        product_id = item.product_id if is_plan else item.build.version.product_id
        folder = by_id.get(assignments.get(item.pk))
        folder_id = folder.pk if folder and folder.product_id == product_id else None
        parent_id = hierarchy.parents.get(item.pk) if is_plan else None
        if parent_id in item_ids:
            plan_children[parent_id].append(item)
        else:
            grouped[(product_id, folder_id)].append(item)

    selected = str(data.get("folder", ""))
    selected_folder = next((folder for folder in folders if str(folder.pk) == selected), None)
    selected_product = data.get("product", "") or (
        str(selected_folder.product_id) if selected_folder else ""
    )
    nodes = []

    def location(product_id, folder_id=None):
        params = data.copy()
        for field in ("page", "folder"):
            params.pop(field, None)
        if str(data.get("product", "")) != str(product_id):
            for field in ("version", "product_version", "build"):
                params.pop(field, None)
        params["product"] = str(product_id)
        if folder_id:
            params["folder"] = str(folder_id)
        return search_url + "?" + params.urlencode()

    emitted = set()

    def add_items(product, folder_id, depth, ancestors):
        # Folder placement follows the visible root plan, while plan nesting
        # remains a separate relationship. Each matching item appears once.
        stack = [
            (item, depth, ancestors, paths.get(folder_id) or product.name)
            for item in reversed(grouped[(product.pk, folder_id)])
        ]
        while stack:
            item, item_depth, item_ancestors, parent_path = stack.pop()
            if item.pk in emitted:
                continue
            emitted.add(item.pk)
            name = item.name if is_plan else item.summary
            label = f"{prefix}-{item.pk} · {name}"
            path = parent_path + " / " + label
            nested = plan_children[item.pk] if is_plan else []
            nodes.append(
                dict(
                    type="resource",
                    key=f"t:{item.pk}",
                    ancestors=",".join(item_ancestors),
                    indent=item_depth * 18,
                    label=label,
                    path=path,
                    id=item.pk,
                    product_id=product.pk,
                    has_children=bool(nested),
                    can_move=item.pk in editable and (not is_plan or not hierarchy.parents[item.pk]),
                    child_url=(
                        (reverse("plans-new") + f"?parent={item.pk}")
                        if is_plan and request.user.has_perm("testplans.add_testplan")
                        else ""
                    ),
                    url=item.get_absolute_url(),
                )
            )
            stack.extend(
                (child, item_depth + 1, item_ancestors + [f"t:{item.pk}"], path)
                for child in reversed(nested)
            )

    def add_folders(product, parent_id, depth, ancestors, visited):
        for folder in children[(product.pk, parent_id)]:
            if folder.pk in visited:
                continue
            visited.add(folder.pk)
            key = f"f:{folder.pk}"
            nodes.append(
                dict(
                    type="folder",
                    key=key,
                    ancestors=",".join(ancestors),
                    indent=depth * 18,
                    label=folder.name,
                    path=paths[folder.pk],
                    id=folder.pk,
                    product_id=product.pk,
                    parent_id=folder.parent_id or "",
                    can_manage=can_manage_folder(request.user, folder),
                    has_children=bool(
                        children[(product.pk, folder.pk)] or grouped[(product.pk, folder.pk)]
                    ),
                    active=str(folder.pk) == selected,
                    url=location(product.pk, folder.pk),
                )
            )
            add_folders(product, folder.pk, depth + 1, ancestors + [key], visited)
            add_items(product, folder.pk, depth + 1, ancestors + [key])

    for product in products:
        key = f"p:{product.pk}"
        nodes.append(
            dict(
                type="product",
                key=key,
                ancestors="",
                indent=0,
                label=product.name,
                path=product.name,
                product_id=product.pk,
                can_manage=not roles.is_read_only(request.user) and request.user.has_perm(permission),
                has_children=any(folder.product_id == product.pk for folder in folders)
                or bool(grouped[(product.pk, None)]),
                active=str(product.pk) == str(selected_product) and not selected,
                url=location(product.pk),
            )
        )
        visited = set()
        add_folders(product, None, 1, [key], visited)
        # Keep legacy cyclic/detached folders accessible without recursive loops.
        for folder in folders:
            if folder.product_id == product.pk and folder.pk not in visited:
                children[(product.pk, None)] = [folder]
                add_folders(product, None, 1, [key], visited)
        add_items(product, None, 1, [key])
    return dict(
        kind=kind,
        title=title,
        template="ai_assistant/_plan_run_directories.html",
        product_rooted=True,
        products=products,
        nodes=nodes,
        items=items,
        prefix=prefix.lower(),
        selected_product=selected_product,
        has_more=has_more,
        display_limit=TREE_LIMIT,
    )


def run_tree_context(request, data):
    from tcms.testplans.plan_library import numeric_id

    query = get_objects_for_user(request.user, "testruns.view_testrun", klass=TestRun)
    query = query.select_related("build__version__product", "plan")
    for key, field in (
        ("product", "build__version__product_id"),
        ("version", "build__version_id"),
        ("build", "build_id"),
    ):
        if data.get(key):
            query = query.filter(**{field: numeric_id(data[key])})
    tree = build_tree(request, "run", query.order_by("-pk"), data)
    invalid = False
    for key, model in (("product", Product), ("version", Version), ("build", Build)):
        if not data.get(key):
            continue
        selected = model.objects.filter(pk=numeric_id(data[key])).first()
        invalid = invalid or selected is None
        if selected and key == "version" and data.get("product"):
            invalid = invalid or selected.product_id != numeric_id(data["product"])
        if selected and key == "build":
            if data.get("product"):
                invalid = invalid or selected.version.product_id != numeric_id(data["product"])
            if data.get("version"):
                invalid = invalid or selected.version_id != numeric_id(data["version"])
    tree["invalid_scope"] = invalid
    return tree
