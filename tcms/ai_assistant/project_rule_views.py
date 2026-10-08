"""Shared project rules and explicitly preserved legacy account rules."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from . import roles
from .models import ProjectAIRuleBinding
from .project_rules import RuleForm, can_manage, managed_products, visible_profiles, save_rule, publish_legacy, toggle_rule, SECTION_LABELS


@login_required
@require_GET
def index(request):
    products = roles.member_products(request.user)
    shared = ProjectAIRuleBinding.objects.filter(product__in=products).select_related("product", "profile", "published_by")
    legacy = visible_profiles(request.user).filter(project_binding__isnull=True).select_related("product", "owner")
    product_id = request.GET.get("product", "")
    if product_id.isdecimal() and len(product_id) < 19:
        shared = shared.filter(product_id=product_id)
        legacy = legacy.filter(product_id=product_id)
    state = request.GET.get("state", "")
    if state in ("active", "inactive"):
        shared = shared.filter(profile__is_active=state == "active")
    for item in shared:
        item.editable = can_manage(request.user, item.product)
    for profile in legacy:
        profile.can_publish = bool(profile.product_id and can_manage(request.user, profile.product)
            and (request.user.is_superuser or profile.owner_id == request.user.pk))
    return render(request, "ai_assistant/instruction_profiles.html", {
        "shared_rules": shared, "legacy_rules": legacy, "projects": products,
        "selected_product_id": product_id, "selected_state": state,
        "can_create": managed_products(request.user).exists(),
    })


@login_required
@require_http_methods(["GET", "POST"])
def edit(request, pk=None):
    profile = get_object_or_404(visible_profiles(request.user).select_related("product"), pk=pk) if pk else None
    published = bool(profile and ProjectAIRuleBinding.objects.filter(profile=profile).exists())
    editable = can_manage(request.user, profile.product) if published else bool(not profile and managed_products(request.user).exists())
    if request.method == "POST" and not editable:
        raise PermissionDenied
    if not profile and not editable:
        raise PermissionDenied
    form = RuleForm(request.POST or None, user=request.user, profile=profile,
        initial={"product": request.GET.get("product")})
    if profile and not editable:
        # A member can inspect shared content but has no write authority.
        form.fields["product"].queryset = roles.member_products(request.user)
        for field in form.fields.values():
            field.disabled = True
    if request.method == "POST" and form.is_valid():
        try:
            profile = save_rule(request.user, form.cleaned_data, profile)
            messages.success(request, f"AI 测试规则已保存为 V{profile.version}。")
            return redirect("ai_assistant:instruction_profiles")
        except (ValueError, IntegrityError) as exc:
            form.add_error(None, str(exc) if isinstance(exc, ValueError) else "规则已被其他用户创建，请刷新列表。")
    return render(request, "ai_assistant/edit_instruction_profile.html", {
        "form": form, "profile": profile, "editable": editable, "published": published,
    })


@login_required
@require_POST
def toggle(request, pk):
    profile = get_object_or_404(visible_profiles(request.user), pk=pk)
    try:
        toggle_rule(request.user, profile, request.POST.get("version"))
        messages.success(request, "规则状态已更新，后续 AI 任务使用新版本。")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("ai_assistant:instruction_profiles")


@login_required
@require_POST
def publish(request, pk):
    profile = get_object_or_404(visible_profiles(request.user), pk=pk)
    if profile.product_id is None:
        messages.error(request, "旧规则没有关联项目，暂不能发布为项目共享规则。")
    else:
        try:
            publish_legacy(request.user, profile)
            messages.success(request, "已发布为项目共享规则；原有内容和版本保持不变。")
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect("ai_assistant:instruction_profiles")


@login_required
@require_GET
def history(request, pk):
    profile = get_object_or_404(visible_profiles(request.user), pk=pk)
    revisions = list(profile.revisions.select_related("changed_by"))
    for revision in revisions:
        revision.sections = [(label, revision.snapshot.get("sections", {}).get(key, "")) for key, label in SECTION_LABELS]
    return render(request, "ai_assistant/instruction_history.html", {"profile": profile, "revisions": revisions})
