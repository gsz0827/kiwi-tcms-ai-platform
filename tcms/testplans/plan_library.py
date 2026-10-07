"""Read-only plan summaries; execution results remain on their original runs."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.utils import timezone
from guardian.shortcuts import get_objects_for_user

from tcms.management.models import Product, Version
from tcms.testplans.models import PlanType, TestPlan
from tcms.testplans.forms import SearchPlanForm
from tcms.testcases.models import TestCasePlan
from tcms.testruns.models import TestRun


def numeric_id(value):
    value = str(value or "")
    return int(value) if value.isascii() and value.isdecimal() and len(value) <= 18 else None


def visible_runs(user):
    return get_objects_for_user(user, "testruns.view_testrun", klass=TestRun)


def display_zone(request):
    return ZoneInfo(getattr(getattr(request, "ui_preference", None), "time_zone", "Asia/Shanghai"))


def local_display(value, zone):
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, ZoneInfo(settings.TIME_ZONE))
    return value.astimezone(zone)


def plan_list_context(request):
    # Explicit empty choices override top-bar defaults. Invalid/stale IDs fail closed.
    data = request.GET.copy()
    if "product" not in data:
        data["product"] = str(request.session.get("ai_product_id", "") or "")
        if "product_version" not in data and "version" not in data:
            data["product_version"] = str(request.session.get("ai_version_id", "") or "")
    if "version" in data and "product_version" not in data:
        data["product_version"] = data["version"]
    from tcms.testplans.plan_hierarchy import visible_plans, filter_plan_folder

    query = visible_plans(request.user).select_related("product", "product_version", "author", "type")
    products = Product.objects.order_by("name", "pk")
    selected_product = products.filter(pk=numeric_id(data.get("product"))).first()
    if data.get("product"):
        query = query.filter(product=selected_product) if selected_product else query.none()
    all_versions = Version.objects.select_related("product").order_by("product__name", "value")
    versions = all_versions
    if selected_product:
        versions = versions.filter(product=selected_product)
    if data.get("product_version"):
        version = versions.filter(pk=numeric_id(data["product_version"])).first()
        query = query.filter(product_version=version) if version else query.none()
    term = data.get("name", "").strip()
    if term:
        plan_id = numeric_id(term.removeprefix("TP-").removeprefix("tp-"))
        query = query.filter(Q(name__icontains=term) | Q(pk=plan_id))
    status = data.get("status", "active")
    if status != "all":
        query = query.filter(is_active=status == "active")
    if data.get("type"):
        query = query.filter(type_id=numeric_id(data["type"]))
    if data.get("author"):
        query = query.filter(author__username__icontains=data["author"].strip())
    if data.get("default_tester"):
        query = query.filter(
            cases__default_tester__username__icontains=data["default_tester"].strip()
        )
    for tag in (value.strip() for value in data.get("tag", "").split(",")):
        if tag:
            query = query.filter(tag__name=tag)
    errors = []
    zone = display_zone(request)
    for key, lookup in (("after", "create_date__gte"), ("before", "create_date__lt")):
        if data.get(key):
            try:
                date = parse_date(data[key])
            except ValueError:
                date = None
            if date:
                if key == "before":
                    date += timedelta(days=1)
                boundary = datetime.combine(date, time.min, tzinfo=zone)
                if not settings.USE_TZ:
                    boundary = boundary.astimezone(ZoneInfo(settings.TIME_ZONE)).replace(tzinfo=None)
                query = query.filter(**{lookup: boundary})
            else:
                errors.append("创建日期格式不正确，请重新选择。")
                query = query.none()
    query = query.distinct().order_by("-pk")
    from tcms.ai_assistant.plan_run_directories import build_tree

    case_count = (
        TestCasePlan.objects.filter(plan_id=OuterRef("pk"))
        .order_by()
        .values("plan_id")
        .annotate(total=Count("pk"))
        .values("total")
    )
    query = query.annotate(case_total=Coalesce(Subquery(case_count, output_field=IntegerField()), 0))
    browser = build_tree(request, "plan", query, data)
    params = data.copy()
    params.pop("page", None)
    params.pop("folder", None)
    params.pop("version", None)
    browser["clear_folder_url"] = reverse("plans-search") + "?" + params.urlencode()
    browser["selected_folder"] = data.get("folder", "")
    request.plan_browser = browser
    folder_id = data.get("folder", "")
    if folder_id and numeric_id(folder_id) is None:
        query = query.none()
    else:
        query = filter_plan_folder(query, request, folder_id, selected_product)
    run_count = (
        visible_runs(request.user)
        .filter(plan_id=OuterRef("pk"))
        .order_by()
        .values("plan_id")
        .annotate(total=Count("pk"))
        .values("total")
    )
    query = query.annotate(
        case_total=Coalesce(Subquery(case_count, output_field=IntegerField()), 0),
        run_total=Coalesce(Subquery(run_count, output_field=IntegerField()), 0),
    )
    page = Paginator(query, 20).get_page(request.GET.get("page"))
    for plan in page:
        plan.display_created = local_display(plan.create_date, zone)
    params = data.copy()
    params.pop("page", None)
    form = SearchPlanForm(data)
    form.populate(product_id=selected_product.pk if selected_product else None)
    return {
        "form": form,
        "plans": page,
        "filters": data,
        "selected_status": status,
        "products": products,
        "versions": versions,
        "plan_types": PlanType.objects.all(),
        "page_query": params.urlencode(),
        "filter_errors": errors,
        "advanced_open": any(
            data.get(key) for key in ("type", "author", "default_tester", "tag", "after", "before")
        ),
    }


def plan_detail_context(plan, request):
    user = request.user
    zone = display_zone(request)
    from tcms.testplans.plan_hierarchy import rollup_context

    rollup = rollup_context(plan, request)
    direct_runs = visible_runs(user).filter(plan=plan).select_related("manager", "build", "plan")
    include_children = request.GET.get("run_scope") == "family"
    runs = rollup.pop("plan_rollup_runs") if include_children else direct_runs
    page = Paginator(runs.order_by("-pk"), 20).get_page(request.GET.get("run_page"))
    from tcms.web_testing.models import WebRun
    web_sources = {item.test_run_id: item for item in WebRun.objects.filter(owner=user, test_run_id__in=[item.pk for item in page]).order_by("created")}
    for run in page:
        source = web_sources.get(run.pk)
        run.web_execution_url = reverse("web_testing:run", args=[source.pk]) if source else ""
        run.web_execution_status = source.get_status_display() if source else ""
        run.display_start = local_display(run.start_date, zone)
        run.display_stop = local_display(run.stop_date, zone)
    return {
        "plan_case_count": plan.cases.count(),
        "plan_confirmed_count": plan.cases.filter(case_status__is_confirmed=True).count(),
        **rollup,
        "plan_run_scope": "family" if include_children else "self",
        "plan_run_count": direct_runs.count(),
        "plan_open_run_count": direct_runs.filter(stop_date__isnull=True).count(),
        "plan_runs": page,
        "plan_display_created": local_display(plan.create_date, zone),
        "can_edit_plan": user.has_perm("testplans.change_testplan", plan)
        or user.has_perm("testplans.change_testplan"),
    }
