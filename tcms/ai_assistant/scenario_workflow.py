"""Business-first repository; execution implementations never replace scenario identity."""

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from guardian.shortcuts import assign_perm, get_objects_for_user

from tcms.management.models import Product
from tcms.testruns.models import TestRun, TestExecution
from tcms.web_testing.models import WebCase, WebRun, WebSuite
from . import roles
from tcms.testcases.models import TestCase
from .edit_safety import guard_edit, valid_edit
from .product_case_tree import build_tree, search_cases
from .case_library import visible_cases
from .case_directories import filter_cases
from .crypto import decrypt_api_key
from .models import APICase, APISuite, APIResult, AITestCaseDraft
from .scenario_permissions import can_edit_scenario, editable_scenarios
from .scenario_design_forms import ScenarioForm, StepFormSet
from .scenario_design import CaseDesign, parse_design, serialize_design
from .case_design_context import manual_context
from .manual_design_capture import lock_manual_source, capture_manual_design
from .scenario_creation import (
    can_create,
    new_url,
    add_directory_field,
    assign_created_case,
    case_library_url,
    choose_product,
)


def list_url(product=None):
    return reverse("ai_assistant:scenario_library") + (f"?product={product.pk}" if product else "")


@login_required
@require_GET
def new(request):
    return choose_product(request)


@login_required
@require_GET
@never_cache
def preview(request, pk):
    case = get_object_or_404(
        visible_cases(request.user).select_related("category__product", "priority", "case_status"),
        pk=pk,
    )
    return render(
        request,
        "ai_assistant/scenario_tree_preview.html",
        {"case": case, "can_edit": can_edit_scenario(request.user, case)},
    )


@login_required
@never_cache
def library(request):
    products = Product.objects.order_by("name")
    raw = str(request.GET.get("product", request.session.get("ai_product_id", "")))
    product = get_object_or_404(products, pk=raw) if raw.isdigit() else None
    term = request.GET.get("q", "").strip()[:200]
    mode = request.GET.get("automation", "")
    from .models import AutomationArchive

    archived_cases = TestExecution.objects.filter(
        run_id__in=AutomationArchive.objects.values("test_run_id")
    ).values("case_id")
    query = (
        visible_cases(request.user)
        .exclude(pk__in=archived_cases, text__startswith="自动化归档快照\n来源：")
        .select_related("category__product", "case_status", "priority")
    )
    query = query.annotate(
        owned_web=Exists(
            WebCase.objects.filter(
                owner=request.user,
                test_case_id=OuterRef("pk"),
                product_id=OuterRef("category__product_id"),
            )
        ),
        owned_api=Exists(
            APICase.objects.filter(
                owner=request.user,
                test_case_id=OuterRef("pk"),
                product_id=OuterRef("category__product_id"),
            )
        ),
    )
    if mode == "web":
        query = query.filter(owned_web=True)
    elif mode == "api":
        query = query.filter(owned_api=True)
    elif mode == "none":
        query = query.filter(owned_web=False, owned_api=False)
    else:
        mode = ""
    # The tree spans visible product roots; only the table uses the selected product/folder.
    query = search_cases(query, term)
    tree_query = query
    if product:
        query = query.filter(category__product=product)
    folder = request.GET.get("folder", "")
    if folder != "unfiled":
        query = filter_cases(query, "case", folder, product)
    page = Paginator(query.order_by("-pk"), 30).get_page(request.GET.get("page"))
    browser = build_tree(request, products, tree_query, product, term, mode, list(page))
    editable_ids = set(
        editable_scenarios(request.user)
        .filter(pk__in=[case.pk for case in page])
        .values_list("pk", flat=True)
    )
    for case in page:
        case.can_edit = case.pk in editable_ids
        case.workflow_url = reverse("ai_assistant:scenario_detail", args=[case.pk])
        for key, value in browser["paths"][case.pk].items():
            setattr(case, key, value)
    request.case_hub_browser = browser
    selected_folder = next(
        (
            item
            for item in browser["folders"]
            if str(item.pk) == folder and product and item.product_id == product.pk
        ),
        None,
    )

    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "ai_assistant/scenario_library.html",
        dict(
            products=products,
            product=product,
            cases=page,
            q=term,
            mode=mode,
            query=params.urlencode(),
            can_create=can_create(request.user),
            create_url=new_url(product, selected_folder) if product else None,
            unlinked_web=WebCase.objects.filter(owner=request.user, test_case__isnull=True)
            .filter(**({"product": product} if product else {}))
            .count(),
        ),
    )


@login_required
@never_cache
@guard_edit(TestCase)
def edit(request, product_id=None, pk=None):
    case = (
        get_object_or_404(visible_cases(request.user).select_related("category__product"), pk=pk)
        if pk
        else None
    )
    product = case.category.product if case else get_object_or_404(Product, pk=product_id)
    design_source, design_tasks, design_snapshot = (
        manual_context(request, product) if case is None else (None, [], None)
    )
    if (
        roles.is_read_only(request.user)
        or not request.user.is_active
        or (
            not can_edit_scenario(request.user, case)
            if case
            else not request.user.has_perm("testcases.add_testcase")
        )
    ):
        raise PermissionDenied
    form = ScenarioForm(
        request.POST if request.method == "POST" else None, instance=case, product=product
    )
    if case is None:
        add_directory_field(form, request, product)
    if design_source and not form.is_bound:
        form.initial["category"] = design_source.category_id
        form.initial["requirement"] = (f"R-{design_source.pk} {design_source.title}")[
            : form.fields["requirement"].max_length
        ]
    steps = StepFormSet(
        request.POST if request.method == "POST" else None,
        prefix="steps",
        initial=form.design.steps or [{}],
    )
    form_valid = (
        (form.is_valid() and valid_edit(request, form)) if request.method == "POST" else False
    )
    steps_valid = steps.is_valid() if request.method == "POST" and form.mode == "structured" else True
    if (
        design_source
        and request.method == "POST"
        and request.POST.get("design_fingerprint") != design_snapshot["fingerprint"]
    ):
        form.add_error(None, "需求或开发文档已变更，请刷新后重新核对用例。")
        form_valid = False
    form_context = dict(
        form=form,
        steps=steps,
        product=product,
        title="编辑业务用例" if case else "新建用例",
        back_url=(
            reverse("ai_assistant:case_design", args=[design_source.pk])
            if design_source
            else list_url(product)
            + (
                f"&folder={form.initial['folder']}"
                if case is None and form.initial.get("folder")
                else ""
            )
        ),
        design_source=design_source,
        design_tasks=design_tasks,
        design_snapshot=design_snapshot,
    )
    if request.method == "POST" and form_valid and steps_valid:
        try:
            with transaction.atomic():
                if design_source:
                    try:
                        design_source, design_tasks = lock_manual_source(
                            request.user, design_source, design_snapshot
                        )
                    except ValueError as exc:
                        form.add_error(None, str(exc))
                        return render(request, "ai_assistant/scenario_form.html", form_context)
                saved = form.save(commit=False)
                if form.mode == "structured":
                    design = CaseDesign(
                        **{
                            key: form.cleaned_data[key]
                            for key in ("test_type", "case_number", "preconditions", "extra")
                        },
                        steps=[
                            {key: step[key] for key in ("action", "data", "expected")}
                            for step in steps.cleaned_data
                            if step and not step.get("DELETE")
                        ],
                    )
                    saved.text = serialize_design(design)
                if case is None:
                    saved.author = request.user
                    saved.is_automated = False
                saved._history_user = request.user
                saved.save()
                if case is None:
                    assign_created_case(request, saved, form.cleaned_data["folder"])
                if case is None and design_source:
                    capture_manual_design(design_source, design_tasks, design_snapshot, saved)
                if case is None:
                    for permission in ("view_testcase", "change_testcase"):
                        assign_perm(permission, request.user, saved)
        except ValueError as exc:
            if case is not None:
                raise
            # A rolled-back insert leaves its Python instance with a generated
            # PK. Restore new-form state so directory errors stay visible.
            form.instance.pk = None
            form.instance._state.adding = True
            form.add_error("folder", str(exc))
            return render(request, "ai_assistant/scenario_form.html", form_context)
        messages.success(request, "业务用例已保存；可人工执行，或按需添加自动化脚本。")
        return redirect("ai_assistant:scenario_detail", pk=saved.pk)

    return render(
        request,
        "ai_assistant/scenario_form.html",
        form_context,
    )


@login_required
@never_cache
def detail(request, pk):
    case = get_object_or_404(
        visible_cases(request.user).select_related(
            "category__product", "priority", "case_status", "author"
        ),
        pk=pk,
    )
    product = case.category.product
    web = list(
        WebCase.objects.filter(owner=request.user, product=product, test_case=case).defer(
            "steps_encrypted"
        )
    )
    api = list(
        APICase.objects.filter(owner=request.user, product=product, test_case=case).only(
            "pk", "name", "product_id", "test_case_id"
        )
    )
    source = (
        AITestCaseDraft.objects.filter(
            imported_case=case, request__in=roles.visible_requests(request.user)
        )
        .select_related("request")
        .first()
    )
    runs = get_objects_for_user(request.user, "testruns.view_testrun", klass=TestRun)
    executions = list(
        TestExecution.objects.filter(case=case, run__in=runs)
        .select_related("run", "status")
        .order_by("-pk")[:10]
    )
    # New runs carry immutable business identity; do not infer old runs from today's bindings.
    recent_web = []
    web_positions = {}
    for run in (
        WebRun.objects.filter(owner=request.user, product=product)
        .defer("error")
        .order_by("-created")[:20]
    ):
        try:
            snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
            positions = {
                index
                for index, item in enumerate(snapshot.get("cases", []), 1)
                if item.get("business_case_id") == case.pk
            }
            if positions:
                recent_web.append(run)
                web_positions[run.pk] = positions
        except (ValueError, TypeError, KeyError):
            continue
    web_suites = list(
        WebSuite.objects.filter(owner=request.user, product=product).defer("datasets_encrypted")[:100]
    )
    web_suites = [suite for suite in web_suites if set(suite.case_ids) & {item.pk for item in web}]
    api_suites = list(
        APISuite.objects.filter(owner=request.user, product=product).only("pk", "name", "case_ids")[
            :100
        ]
    )
    api_suites = [suite for suite in api_suites if set(suite.case_ids) & {item.pk for item in api}]
    api_results = (
        APIResult.objects.filter(test_case=case, run__owner=request.user, run__product=product)
        .select_related("run")
        .only("pk", "status", "position", "run_id", "run__created", "run__product_id")
        .order_by("-pk")[:10]
    )
    from .scenario_trace import archived_trace

    archives, published_ids = archived_trace(request.user, web_positions, api_results)
    defects = (
        roles.visible_defects(request.user)
        .filter(Q(execution__case=case) | Q(execution_id__in=published_ids))
        .select_related("execution")[:10]
    )
    return render(
        request,
        "ai_assistant/scenario_detail.html",
        dict(
            case=case,
            design=parse_design(case.text),
            product=product,
            can_edit=can_edit_scenario(request.user, case),
            web_configs=web,
            api_configs=api,
            source=source,
            source_outdated=source is not None
            and (source.needs_update or source.requirement_version != source.request.version),
            latest_recheck=(
                case.source_reviews.filter(request_id=source.request_id)
                .select_related("reviewed_by")
                .first()
                if source
                else None
            ),
            executions=executions,
            web_runs=recent_web,
            web_suites=web_suites,
            back_url=case_library_url(case, request.user),
            api_suites=api_suites,
            api_results=api_results,
            defects=defects,
            archives=archives,
        ),
    )
