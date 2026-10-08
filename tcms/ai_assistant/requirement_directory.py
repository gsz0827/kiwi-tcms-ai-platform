"""Product-rooted requirement presentation; existing access and assignment rules remain intact."""

from collections import defaultdict

from django.urls import reverse

from . import roles
from .models import ProjectResourceAssignment

LIMIT = 200


def requirement_browser(request, queryset, selected_product):
    from .templatetags.ai_navigation import _build_resource_browser

    browser = _build_resource_browser(
        request,
        "requirement",
        "需求目录",
        "fa-lightbulb-o",
        queryset.prefetch_related("dev_tasks"),
        lambda item: (
            f"{item.category.product.name} / {item.category.name}"
            if item.category_id
            else "未指定项目"
        ),
        limit=LIMIT,
    )
    browser.update(product_rooted=True, selected_product=str(selected_product), limit=LIMIT)
    products = list(browser["products"])
    folders = browser["folder_nodes"]
    by_id = {folder.pk: folder for folder in folders}
    children = defaultdict(list)
    for folder in folders:
        parent = by_id.get(folder.parent_id)
        if parent is None or parent.product_id != folder.product_id:
            children[(folder.product_id, None)].append(folder)
        else:
            children[(folder.product_id, parent.pk)].append(folder)
    for values in children.values():
        values.sort(key=lambda folder: (folder.position, folder.name.casefold(), folder.pk))

    assignments = {
        row.object_id: row.folder_id
        for row in ProjectResourceAssignment.objects.filter(
            resource_type="requirement", object_id__in=[item.pk for item in browser["items"]]
        )
    }
    grouped = defaultdict(list)
    for item in browser["items"]:
        product_id = item.category.product_id if item.category_id else None
        folder = by_id.get(assignments.get(item.pk))
        folder_id = folder.pk if folder and folder.product_id == product_id else None
        grouped[(product_id, folder_id)].append(item)

    nodes = []
    writable = request.user.is_active and not roles.is_read_only(request.user)
    manageable_products = {product.pk for product in products
                           if roles.can_manage_requirement_directories(request.user, product)}

    def append_items(product_id, folder_id, ancestors, path):
        for item in grouped.get((product_id, folder_id), ()):
            full_path = f"{path} / R-{item.pk} · {item.title}"
            nodes.append(
                {
                    "kind": "requirement",
                    "key": f"r:{item.pk}",
                    "pk": item.pk,
                    "name": item.title,
                    "number": f"R-{item.pk}",
                    "item": item,
                    "ancestors": ",".join(ancestors),
                    "indent": len(ancestors) * 18,
                    "search": full_path,
                    "path": full_path,
                    "product_id": product_id,
                    "can_move": writable and product_id in manageable_products,
                }
            )

    for product in products:
        root = f"p:{product.pk}"
        has_children = bool(children.get((product.pk, None)) or grouped.get((product.pk, None)))
        nodes.append(
            {
                "kind": "product",
                "key": root,
                "pk": product.pk,
                "name": product.name,
                "product_id": product.pk,
                "ancestors": "",
                "indent": 0,
                "path": product.name,
                "search": product.name,
                "has_children": has_children,
                "can_create": product.pk in manageable_products,
                "url": reverse("ai_assistant:index") + f"?product={product.pk}",
            }
        )
        stack = [
            (folder, [root], product.name)
            for folder in reversed(children.get((product.pk, None), ()))
        ]
        visited = set()
        while stack:
            folder, ancestors, parent_path = stack.pop()
            if folder.pk in visited:
                continue
            visited.add(folder.pk)
            key = f"f:{folder.pk}"
            path = f"{parent_path} / {folder.name}"
            nodes.append(
                {
                    "kind": "folder",
                    "key": key,
                    "pk": folder.pk,
                    "name": folder.name,
                    "product_id": product.pk,
                    "ancestors": ",".join(ancestors),
                    "indent": len(ancestors) * 18,
                    "path": path,
                    "search": path,
                    "has_children": bool(
                        children.get((product.pk, folder.pk)) or grouped.get((product.pk, folder.pk))
                    ),
                    "can_manage": folder.can_quick_manage,
                }
            )
            append_items(product.pk, folder.pk, ancestors + [key], path)
            stack.extend(
                (child, ancestors + [key], path)
                for child in reversed(children.get((product.pk, folder.pk), ()))
            )
        append_items(product.pk, None, [root], product.name)
        # Corrupt legacy parent chains must not hide otherwise visible requirements.
        for folder in folders:
            if folder.product_id == product.pk and folder.pk not in visited:
                append_items(product.pk, folder.pk, [root], product.name)

    if grouped.get((None, None)):
        nodes.append(
            {
                "kind": "unassigned",
                "key": "u",
                "name": "未指定项目",
                "ancestors": "",
                "indent": 0,
                "search": "未指定项目",
                "has_children": True,
            }
        )
        append_items(None, None, ["u"], "未指定项目")
    browser["nodes"] = nodes
    return browser
