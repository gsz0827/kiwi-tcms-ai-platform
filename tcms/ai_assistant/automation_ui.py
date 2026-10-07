"""Shared automation presentation and owner-scoped list navigation."""

import uuid
from datetime import datetime, time, timedelta
from functools import wraps
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from zoneinfo import ZoneInfo
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.dateparse import parse_date

from tcms.management.models import Product, Version
from tcms.testplans.plan_library import display_zone, numeric_id
from .models import APICase, APIEnvironment, APIRun, APISuite
from .roles import is_read_only


def write_guard(view):
    @wraps(view)
    def guarded(request, *args, **kwargs):
        if request.method == "POST" and (not request.user.is_active or is_read_only(request.user)):
            return HttpResponse("只读账号不能修改或执行测试。", status=403)
        return view(request, *args, **kwargs)

    return guarded


def home_url(product, tab="environments"):
    return reverse("ai_assistant:api_home") + "?" + urlencode({"product": product.pk, "tab": tab})


def return_url(request, fallback):
    candidate = request.POST.get("return_to", request.GET.get("return_to", ""))
    allowed = {
        reverse(name)
        for name in (
            "ai_assistant:api_home",
            "web_testing:cases",
            "web_testing:suites",
            "web_testing:runs",
            "web_testing:environments",
        )
    }
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return fallback
    if (
        len(candidate) > 3000
        or parsed.scheme
        or parsed.netloc
        or parsed.path not in allowed
        or not candidate.startswith("/")
        or any(c in candidate for c in ("\\", "\r", "\n"))
    ):
        return fallback
    return candidate


def api_list_context(request):
    from .case_library import selected_product
    from .automation_folders import automation_cases

    products, product = selected_product(request)
    tab = request.GET.get("tab", "environments")
    if tab not in ("cases", "suites", "runs", "environments"):
        tab = "environments"
    # Configuration trees remain rooted in a concrete project.
    if tab != "cases" and request.GET.get("product") == "":
        product = None
    data = request.GET.copy()
    data["tab"] = tab
    data["product"] = str(product.pk) if product else ""
    q = data.get("q", "").strip()[:200]
    errors = []
    states = APIRun.STATUSES
    versions = Version.objects.select_related("product").order_by("value", "pk")
    if product:
        versions = versions.filter(product=product)
    model = {"cases": APICase, "suites": APISuite, "runs": APIRun, "environments": APIEnvironment}[
        tab
    ]
    items = model.objects.filter(owner=request.user).select_related("product")
    if product:
        items = items.filter(product=product)
    if tab == "cases":
        items = automation_cases(request.user, "api_case", product, data).select_related("test_case")
    elif tab == "runs":
        items = items.select_related("suite", "test_run__plan", "test_run__build__version").annotate(
            result_total=Count("results"),
            result_finished=Count("results", filter=~Q(results__status="pending")),
        )
        if q:
            lookup = (
                Q(environment_name__icontains=q)
                | Q(suite__name__icontains=q)
                | Q(test_run__summary__icontains=q)
            )
            try:
                lookup |= Q(pk=uuid.UUID(q))
            except ValueError:
                pass
            items = items.filter(lookup)
        if "version" not in data:
            value = request.session.get("ai_version_id", "")
            data["version"] = str(value) if versions.filter(pk=numeric_id(value)).exists() else ""
        if data.get("version"):
            version = versions.filter(pk=numeric_id(data["version"])).first()
            items = items.filter(test_run__build__version=version) if version else items.none()
        if data.get("status"):
            items = (
                items.filter(status=data["status"])
                if data["status"] in dict(states)
                else items.none()
            )
        zone = display_zone(request)
        for field, lookup in (("after", "created__gte"), ("before", "created__lt")):
            if not data.get(field):
                continue
            try:
                value = parse_date(data[field])
            except ValueError:
                value = None
            if value is None or not 2 <= value.year <= 9998:
                errors.append("日期格式不正确，请重新选择。")
                items = items.none()
            else:
                if field == "before":
                    value += timedelta(days=1)
                boundary = datetime.combine(value, time.min, tzinfo=zone)
                if not settings.USE_TZ:
                    boundary = boundary.astimezone(ZoneInfo(settings.TIME_ZONE)).replace(tzinfo=None)
                items = items.filter(**{lookup: boundary})
    else:
        if q:
            lookup = Q(name__icontains=q)
            if q.isascii() and q.isdecimal() and len(q) <= 18:
                lookup |= Q(pk=int(q))
            items = items.filter(lookup)
        if tab == "suites":
            items = items.select_related("environment")
    order = (
        ("sequence", "pk")
        if tab == "cases"
        else (
            ("-created", "-pk")
            if tab == "runs"
            else ("-updated", "-pk") if tab == "suites" else ("name", "pk")
        )
    )
    page_param = "case_page" if tab == "cases" else "page"
    page = Paginator(items.order_by(*order), 30).get_page(data.get(page_param))
    params = data.copy()
    params.pop(page_param, None)
    title = {
        "cases": "自动化脚本",
        "suites": "测试套件",
        "runs": "执行任务",
        "environments": "测试环境",
    }[tab]
    context = {
        tab: page,
        "page": page,
        "page_param": page_param,
        "case_query": params.urlencode(),
        "query": params.urlencode(),
        "products": products,
        "product": product,
        "tab": tab,
        "title": title,
        "filters": data,
        "run_filter": tab == "runs",
        "run_states": states,
        "versions": versions,
        "filter_errors": errors,
        "reset_url": (
            home_url(product, tab)
            if product
            else reverse("ai_assistant:api_home") + "?product=&tab=" + tab
        ),
        "can_write": not is_read_only(request.user),
        "single_project": tab == "cases",
        "request_methods": APICase.METHODS if tab == 'cases' else [],
    }
    if product:
        names = {
            "cases": ("api_case_new", "新建脚本"),
            "suites": ("api_suite_new", "新建套件"),
            "runs": ("api_submit", "新建执行任务"),
            "environments": ("api_environment_new", "新建环境"),
        }
        route, label = names[tab]
        context.update(
            create_url=reverse("ai_assistant:" + route, args=[product.pk]), create_label=label
        )
        if tab == "cases":
            from .case_directories import can_manage_common
            if request.user.has_perm('testcases.add_testcase') and can_manage_common(request.user, product):
                context['import_url'] = reverse('ai_assistant:postman_upload', args=[product.pk])
            context.update(
                ai_url=reverse("ai_assistant:api_ai_generate", args=[product.pk]),
                ai_label="AI 生成接口脚本",
            )
    return context
