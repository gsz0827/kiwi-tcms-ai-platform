"""Product roots and shared folders; unassigned business cases belong to product roots."""

from collections import defaultdict
from urllib.parse import urlencode
import re

from django.db.models import Q
from django.urls import reverse
from .case_directories import can_manage_common, can_manage_folder, visible_folders, folder_types
from .models import ProjectResourceAssignment
from .scenario_permissions import editable_scenarios

TREE_LIMIT = 2000


def search_cases(query, term):
    if not term:
        return query
    match = re.fullmatch(r"(?:TC-)?(\d{1,18})", term, flags=re.IGNORECASE)
    condition = Q(summary__icontains=term)
    if match:
        condition |= Q(pk=int(match[1]))
    return query.filter(condition)


def _url(params):
    return reverse("ai_assistant:scenario_library") + "?" + urlencode(params)


def folder_paths(folders):
    lookup = {node.pk: node for node in folders}
    result = {}
    for folder in folders:
        names, seen, current = [], set(), folder
        while current and current.pk not in seen and current.product_id == folder.product_id:
            seen.add(current.pk)
            names.append(current.name)
            current = lookup.get(current.parent_id)
        result[folder.pk] = " / ".join([folder.product.name] + list(reversed(names)))
    return result


def case_paths(cases, folders, paths):
    """Only a valid visible same-product folder can contribute to a displayed path."""
    assignments = {
        item.object_id: item.folder_id
        for item in ProjectResourceAssignment.objects.filter(
            resource_type="case",
            object_id__in=[case.pk for case in cases],
            folder_id__in=paths,
        )
    }
    lookup = {node.pk: node for node in folders}
    result = {}
    for case in cases:
        product = case.category.product
        folder = lookup.get(assignments.get(case.pk))
        if folder and folder.product_id != product.pk:
            folder = None
        directory = paths[folder.pk] if folder else product.name
        result[case.pk] = dict(
            folder_id=folder.pk if folder else None,
            folder_name=folder.name if folder else product.name,
            directory_path=directory,
            full_path=f"{directory} / TC-{case.pk} · {case.summary}",
        )
    return result


def build_tree(request, products, query, product, term, mode, page_cases, *, read_only=False):
    from .scenario_creation import can_create, new_url

    allow_case_creation = not read_only and can_create(request.user)
    products = list(products)
    folders = list(
        visible_folders(request.user)
        .filter(resource_type__in=folder_types("case"))
        .select_related("product")
        .order_by("position", "name", "pk")
    )
    paths = folder_paths(folders)
    folder_map = {node.pk: node for node in folders}
    records = list(
        query.order_by("summary", "pk").values("pk", "summary", "category__product_id")[:TREE_LIMIT]
    )
    ids = [record["pk"] for record in records]
    writable = (
        set()
        if read_only
        else set(editable_scenarios(request.user).filter(pk__in=ids).values_list("pk", flat=True))
    )
    assignments = {
        assignment.object_id: assignment.folder_id
        for assignment in ProjectResourceAssignment.objects.filter(
            resource_type="case", object_id__in=ids, folder_id__in=paths
        )
    }
    case_groups, children = defaultdict(list), defaultdict(list)
    for record in records:
        product_id = record["category__product_id"]
        folder = folder_map.get(assignments.get(record["pk"]))
        folder_id = folder.pk if folder and folder.product_id == product_id else None
        case_groups[(product_id, folder_id)].append(record)
    for folder in folders:
        parent = folder_map.get(folder.parent_id)
        parent_id = parent.pk if parent and parent.product_id == folder.product_id else None
        children[(folder.product_id, parent_id)].append(folder)
    nodes, visited = [], set()
    page_ids = {case.pk for case in page_cases}
    params = dict(q=term, automation=mode)

    def append_cases(prod, folder_id, depth, ancestors, directory_path):
        for record in case_groups[(prod.pk, folder_id)]:
            pk = record["pk"]
            nodes.append(
                dict(
                    kind="case",
                    key=f"c:{pk}",
                    pk=pk,
                    product=prod,
                    depth=depth,
                    indent=depth * 18,
                    ancestors=",".join(ancestors),
                    name=record["summary"],
                    number=f"TC-{pk}",
                    can_move=pk in writable,
                    path=f"{directory_path} / TC-{pk} · {record['summary']}",
                    url=reverse("ai_assistant:scenario_detail", args=[pk]),
                    preview_url=reverse("ai_assistant:scenario_preview", args=[pk]),
                    modal_id=f"scenario-{pk}" if pk in page_ids else "",
                    search=f"TC-{pk} {pk} {record['summary']}",
                    has_children=False,
                )
            )

    for prod in products:
        root_key = f"p:{prod.pk}"
        root_children = children[(prod.pk, None)]
        nodes.append(
            dict(
                kind="product",
                key=root_key,
                pk=prod.pk,
                product=prod,
                name=prod.name,
                path=prod.name,
                depth=0,
                indent=0,
                ancestors="",
                search=prod.name,
                url=_url(params | {"product": prod.pk}),
                can_create=not read_only and can_manage_common(request.user, prod),
                case_create_url=new_url(prod) if allow_case_creation else "",
                active=bool(product and prod.pk == product.pk and not request.GET.get("folder")),
                has_children=bool(root_children or case_groups[(prod.pk, None)]),
            )
        )
        stack = [(node, 1, [root_key]) for node in reversed(root_children)]
        # Invalid legacy cycles stay visible as root-level folders, without rewriting data.
        extra = [
            node
            for node in folders
            if node.product_id == prod.pk and node.parent_id is not None and not root_children
        ]
        if not stack and extra:
            stack = [(node, 1, [root_key]) for node in reversed(extra)]
        while stack:
            folder, depth, ancestors = stack.pop()
            if folder.pk in visited:
                continue
            visited.add(folder.pk)
            key = f"f:{folder.pk}"
            subs = children[(prod.pk, folder.pk)]
            nodes.append(
                dict(
                    kind="folder",
                    key=key,
                    pk=folder.pk,
                    product=prod,
                    folder=folder,
                    name=folder.name,
                    path=paths[folder.pk],
                    depth=depth,
                    indent=depth * 18,
                    ancestors=",".join(ancestors),
                    search=folder.name,
                    has_children=bool(subs or case_groups[(prod.pk, folder.pk)]),
                    can_manage=not read_only and can_manage_folder(request.user, folder),
                    case_create_url=new_url(prod, folder) if allow_case_creation else "",
                    active=str(folder.pk) == request.GET.get("folder", "")
                    and bool(product and product.pk == prod.pk),
                    url=_url(params | {"product": prod.pk, "folder": folder.pk}),
                )
            )
            append_cases(prod, folder.pk, depth + 1, ancestors + [key], paths[folder.pk])
            stack.extend((node, depth + 1, ancestors + [key]) for node in reversed(subs))
        append_cases(prod, None, 1, [root_key], prod.name)
    return dict(
        kind="scenario_library",
        template="ai_assistant/_scenario_directories.html",
        product_rooted=True,
        product=product,
        products=products,
        folders=folders,
        nodes=nodes,
        q=term,
        mode=mode,
        has_more=query.count() > TREE_LIMIT,
        selected_folder=request.GET.get("folder", ""),
        limit=TREE_LIMIT,
        paths=case_paths(page_cases, folders, paths),
    )
