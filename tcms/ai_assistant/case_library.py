"""One repository entry for a scenario, with optional API execution configurations."""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from guardian.shortcuts import assign_perm, get_objects_for_user

from tcms.management.models import Product, Priority
from tcms.testcases.models import Category, TestCase, TestCaseStatus
from .api_forms import LibraryCaseForm
from .models import APICase


def visible_cases(owner):
    return get_objects_for_user(owner, "testcases.view_testcase", klass=TestCase)


def library_products():
    """用例库项目下拉用的查询集。"""
    return Product.objects.order_by("name")


def selected_product(request, products=None):
    """用例库当前项目：GET 参数指定，否则取第一个。返回 (下拉查询集, 当前项目)。

    页面表格与左侧共享目录都从这里取项目，避免两处口径漂移。
    """
    products = library_products() if products is None else products
    product_id = str(request.GET.get("product", request.session.get("ai_product_id", "")))
    if product_id and product_id.isdigit():
        return products, get_object_or_404(products, pk=product_id)
    return products, products.first()


def library_cases(request, product, user=None):
    """当前筛选条件下的可见用例。

    表格和左侧共享目录共用这一套范围：目录栏必须跟随 type/category/q，
    否则被筛掉的用例又会从目录里冒出来。
    """
    user = user or request.user
    cases = visible_cases(user).none()
    if product is None:
        return cases
    cases = visible_cases(user).filter(category__product=product).annotate(
        has_api=Exists(APICase.objects.filter(test_case_id=OuterRef("pk"))))
    mode = request.GET.get("type", "")
    if mode == "manual":
        cases = cases.filter(is_automated=False, has_api=False)
    elif mode == "automated":
        cases = cases.filter(Q(is_automated=True) | Q(has_api=True))
    category_id = request.GET.get("category", "")
    if category_id.isdigit():
        cases = cases.filter(category_id=category_id)
    if request.GET.get("q"):
        cases = cases.filter(summary__icontains=request.GET["q"][:200])
    from .case_directories import filter_cases
    return filter_cases(cases, "case", request.GET.get("folder", ""), product)


def attach_case(config):
    """Used by owned API creation/demo paths; existing links keep their identity."""
    if config.test_case_id:
        return config.test_case
    category, _ = Category.objects.get_or_create(product=config.product, name="接口自动化")
    status = TestCaseStatus.objects.order_by("is_confirmed", "pk").first()
    priority = Priority.objects.filter(is_active=True).first()
    if not status or not priority:
        raise ValueError("请先完成平台初始化，配置用例状态与优先级。")
    case = TestCase.objects.create(summary=config.name, category=category, author=config.owner,
        priority=priority, case_status=status, is_automated=True,
        text="接口请求与断言见自动化脚本。")
    for permission in ("view_testcase", "change_testcase"):
        assign_perm(permission, config.owner, case)
    config.test_case = case
    config.save(update_fields=("test_case",))
    return case


@login_required
def library(request):
    products, product = selected_product(request)
    cases = library_cases(request, product)
    categories = Category.objects.none()
    if product:
        categories = Category.objects.filter(product=product)
        cases = cases.select_related("category", "priority").prefetch_related(Prefetch(
            "apicase_set", queryset=APICase.objects.filter(owner=request.user, product=product),
            to_attr="api_configs"))
    page = Paginator(cases.order_by("-pk"), 30).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "ai_assistant/api/library.html", dict(products=products, product=product,
        cases=page, categories=categories, mode=request.GET.get("type", ""),
        query=query.urlencode()))


@login_required
def create_case(request, product_id):
    product = get_object_or_404(Product, pk=product_id)
    if not request.user.has_perm("testcases.add_testcase"):
        return HttpResponseForbidden("没有新建测试用例权限。")
    form = LibraryCaseForm(request.POST if request.method == "POST" else None, product=product)
    back_url = reverse("ai_assistant:case_library") + f"?product={product.pk}"
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            case = form.save(commit=False)
            case.author = request.user
            case.is_automated = form.cleaned_data["execution_type"] == "api"
            case.save()
            for permission in ("view_testcase", "change_testcase"):
                assign_perm(permission, request.user, case)
        if case.is_automated:
            return redirect(reverse("ai_assistant:api_case_new", args=[product.pk]) + f"?test_case={case.pk}")
        return redirect(back_url)
    return render(request, "ai_assistant/api/form.html", dict(form=form, product=product,
        title="新建测试用例", back_url=back_url))
