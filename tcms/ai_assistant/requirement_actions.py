"""Optional AI actions on an already saved, permission-scoped requirement."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST
from . import roles
from .jobs import enqueue_ai_job
from .models import AIModelConfig


@login_required
@require_POST
def analyze(request, pk):
    source = get_object_or_404(roles.visible_requests(request.user), pk=pk)
    if not request.user.is_active or not roles.can_generate_cases(request.user, source):
        raise PermissionDenied
    config = AIModelConfig.objects.filter(owner=request.user, is_active=True).first()
    if config is None:
        messages.warning(request, '请先配置自己的默认 AI 模型；已保存的需求不受影响。')
        return redirect('ai_assistant:model_settings')
    with transaction.atomic():
        source = get_object_or_404(roles.visible_requests(request.user).select_for_update(), pk=pk)
        if str(source.version) != request.POST.get('source_version'):
            messages.warning(request, '需求已变更，请刷新后基于最新修订分析。')
            return redirect('ai_assistant:requirement_trace', pk=pk)
        try:
            job, _ = enqueue_ai_job(request.user, 'requirement_analysis',
                {'request_id':pk, 'source_version':source.version,
                 'additional_instructions': request.POST.get('additional_instructions', '')},
                model_config=config, dedupe_key=f'requirement-analysis:{pk}:v{source.version}')
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('ai_assistant:requirement_trace', pk=pk)
    return redirect('ai_assistant:job_detail', pk=job.pk)
