"""Shared folder presentation; automation payloads remain owner-scoped."""
from urllib.parse import urlencode

from django.urls import reverse
from .models import APICase, ProjectResourceAssignment, ProjectResourceFolder


AUTOMATION_TYPES = {"web_case", "api_case"}


def automation_cases(user, kind, product, params, *, filter_folder=True):
    from tcms.web_testing.models import WebCase
    model = WebCase if kind == "web_case" else APICase
    query = model.objects.filter(owner=user, product=product).select_related("product", "owner")
    term = params.get("q", "").strip()[:200]
    if term:
        query = query.filter(name__icontains=term)
    selected = params.get("folder", "") if filter_folder else ""
    assignments = ProjectResourceAssignment.objects.filter(resource_type=kind, folder__product=product)
    if selected == "unfiled":
        query = query.exclude(pk__in=assignments.values("object_id"))
    elif selected:
        nodes = list(ProjectResourceFolder.objects.filter(resource_type=kind, product=product).values_list("pk", "parent_id"))
        ids = {int(selected)} if selected.isdigit() and any(str(pk) == selected for pk, _ in nodes) else set()
        while True:
            expanded = ids | {pk for pk, parent in nodes if parent in ids}
            if expanded == ids:
                break
            ids = expanded
        query = query.filter(pk__in=assignments.filter(folder_id__in=ids).values("object_id"))
    return query.order_by("-pk") if kind == "web_case" else query.order_by("sequence", "pk")


def automation_browser(request, kind, product):
    # Imported lazily to avoid coupling the navigation module's import cycle.
    from .templatetags.ai_navigation import _build_resource_browser
    browser = _build_resource_browser(
        request, kind, "用例目录", "fa-folder-open-o",
        automation_cases(request.user, kind, product, request.GET, filter_folder=False),
        lambda item: item.product.name, folder_product=product,
    )
    params = {"product": product.pk}
    if kind == "api_case":
        params["tab"] = "cases"
    if request.GET.get("q"):
        params["q"] = request.GET["q"][:200]
    browser["all_url"] = "?" + urlencode(params)
    browser["unfiled_url"] = "?" + urlencode(dict(params, folder="unfiled"))
    browser["selected_folder"] = request.GET.get("folder", "")
    for folder in browser["folder_nodes"]:
        folder.filter_url = "?" + urlencode(dict(params, folder=folder.pk))
    for item in browser["items"]:
        item.resource_edit_url = reverse("web_testing:case_edit", args=[item.pk]) if kind == "web_case" else reverse(
            "ai_assistant:api_case_edit", args=[product.pk, item.pk])
    return browser
