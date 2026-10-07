"""Read-only development-document navigation under the existing visibility rules."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import roles


@login_required
@require_GET
@never_cache
def detail(request, pk):
    task = get_object_or_404(
        roles.visible_dev_tasks(request.user).select_related(
            "request__category__product", "owner", "assignee"
        ),
        pk=pk,
    )
    requirement = roles.visible_requests(request.user).filter(pk=task.request_id).first()
    related_tasks = (
        roles.visible_dev_tasks(request.user)
        .filter(request_id=task.request_id)
        .exclude(pk=task.pk)
        .order_by("position", "pk")
    )
    can_assign = request.user.is_active and roles.can_assign_dev_tasks(request.user, task.request)
    product = task.request.category.product if task.request.category_id else None
    assignee_choices = (
        roles.assignable_users(product).order_by("username") if can_assign and product else []
    )
    return render(
        request,
        "ai_assistant/dev_task_detail.html",
        {
            "task": task,
            "can_recheck": requirement is not None and roles.can_edit_dev_task(request.user, task),
            "source_outdated": requirement is not None and (task.needs_update or task.requirement_version != requirement.version),
            "latest_recheck": task.source_reviews.filter(request_id=task.request_id).select_related("reviewed_by").first(),
            "related_requirement": requirement,
            "related_tasks": related_tasks,
            "can_assign": can_assign,
            "assignee_choices": assignee_choices,
            "can_edit": request.user.is_active
            and roles.can_update_dev_task_status(request.user, task),
            "can_design": request.user.is_active
            and requirement is not None
            and roles.can_generate_cases(request.user, requirement),
        },
    )
