"""Owned automation configurations inherit directory membership from business cases."""

from urllib.parse import urlencode

from django.db.models import Exists, OuterRef, Q
from django.urls import reverse
from tcms.management.models import Product
from .case_library import visible_cases
from .case_directories import filter_cases, folder_types, visible_folders
from .models import APICase
from .product_case_tree import build_tree, case_paths, folder_paths, search_cases

AUTOMATION_TYPES = {"web_case", "api_case"}


def _model(kind):
    from tcms.web_testing.models import WebCase

    return WebCase if kind == "web_case" else APICase


def _positive_id(value):
    value = str(value)
    return value.isascii() and value.isdecimal() and len(value) <= 18 and int(value) > 0


def automation_cases(user, kind, product, params, *, filter_folder=True):
    query = (
        _model(kind).objects.filter(owner=user, product=product).select_related("product", "owner")
    )
    if kind == 'api_case' and params.get('method'):
        query = query.filter(method=params['method']) if params['method'] in dict(APICase.METHODS) else query.none()
    term = params.get("q", "").strip()[:200]
    if term:
        lookup = Q(name__icontains=term)
        if kind == 'api_case':
            lookup |= Q(path__icontains=term)
        number = term.upper().removeprefix('WEB-').removeprefix('API-')
        if _positive_id(number):
            lookup |= Q(pk=int(number))
        query = query.filter(lookup)
    if filter_folder:
        if params.get("association") == "unlinked":
            query = query.filter(test_case__isnull=True)
        business = visible_cases(user).filter(category__product=product)
        case_term = params.get("case_q", "").strip()[:200]
        if case_term:
            business = search_cases(business, case_term)
            query = query.filter(test_case_id__in=business.values("pk"))
        selected = params.get("business_case", "")
        if selected:
            query = (
                query.filter(test_case_id__in=business.filter(pk=selected).values("pk"))
                if _positive_id(selected)
                else query.none()
            )
        folder = params.get("folder", "")
        if folder:
            allowed = folder == "unfiled" or (
                _positive_id(folder)
                and visible_folders(user)
                .filter(pk=folder, product=product, resource_type__in=folder_types("case"))
                .exists()
            )
            if not allowed:
                query = query.none()
            else:
                query = query.filter(
                    test_case_id__in=filter_cases(business, "case", folder, product).values("pk")
                )
    return query.order_by("-pk") if kind == "web_case" else query.order_by("sequence", "pk")


def _decorate_paths(user, configurations, folders):
    configs = list(configurations or [])
    business = list(
        visible_cases(user)
        .filter(pk__in=[item.test_case_id for item in configs if item.test_case_id])
        .select_related("category__product")
    )
    lookup = {case.pk: case for case in business}
    paths = case_paths(business, folders, folder_paths(folders))
    for config in configs:
        case = lookup.get(config.test_case_id)
        config.business_case_visible = bool(case and case.category.product_id == config.product_id)
        config.business_path = (
            paths[case.pk]["directory_path"]
            if config.business_case_visible
            else "关联不可访问" if config.test_case_id else "未关联"
        )


def automation_browser(request, kind, product, configurations=None):
    owned = _model(kind).objects.filter(owner=request.user)
    if kind == 'api_case' and request.GET.get('method'):
        method = request.GET['method']
        owned = owned.filter(method=method) if method in dict(APICase.METHODS) else owned.none()
    config_term = request.GET.get("q", "").strip()[:200]
    if config_term:
        lookup = Q(name__icontains=config_term)
        if kind == 'api_case':
            lookup |= Q(path__icontains=config_term)
        number = config_term.upper().removeprefix('WEB-').removeprefix('API-')
        if _positive_id(number):
            lookup |= Q(pk=int(number))
        owned = owned.filter(lookup)
    case_term = request.GET.get("case_q", "").strip()[:200]
    business = (
        visible_cases(request.user)
        .annotate(
            _owned_configuration=Exists(
                owned.filter(test_case_id=OuterRef("pk"), product_id=OuterRef("category__product_id"))
            )
        )
        .filter(_owned_configuration=True)
    )
    business = search_cases(business, case_term)
    browser = build_tree(
        request,
        Product.objects.order_by("name"),
        business,
        product,
        case_term,
        "",
        [],
        read_only=True,
    )
    browser.update(
        kind=kind,
        title="用例目录",
        template="ai_assistant/_automation_business_directories.html",
        config_q=config_term,
        q=case_term,
    )
    base = {"q": config_term, "case_q": case_term}
    if kind == "api_case":
        base["tab"] = "cases"
        if request.GET.get('method'):
            base['method'] = request.GET['method']
    endpoint = reverse("web_testing:cases" if kind == "web_case" else "ai_assistant:api_home")

    def url(**values):
        return endpoint + "?" + urlencode(base | values)

    selected = request.GET.get("business_case", "")
    for node in browser["nodes"]:
        params = {"product": node["product"].pk}
        if node["kind"] == "folder":
            params["folder"] = node["pk"]
        elif node["kind"] == "case":
            params["business_case"] = node["pk"]
            node["active"] = str(node["pk"]) == selected and node["product"].pk == product.pk
        elif selected or request.GET.get("association"):
            node["active"] = False
        node["url"] = url(**params)
    browser["all_url"] = url(product=product.pk)
    browser["unlinked_url"] = url(product=product.pk, association="unlinked")
    browser["unlinked_count"] = (
        _model(kind)
        .objects.filter(owner=request.user, product=product, test_case__isnull=True)
        .count()
    )
    browser["unlinked_active"] = request.GET.get("association") == "unlinked"
    browser["all_active"] = not any(
        request.GET.get(key) for key in ("association", "business_case", "folder")
    )
    browser["manage_url"] = (
        reverse("ai_assistant:scenario_library") + "?" + urlencode({"product": product.pk})
    )
    _decorate_paths(request.user, configurations, browser["folders"])
    return browser
