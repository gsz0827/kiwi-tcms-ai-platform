import json
import re
import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from tcms.ai_assistant.api_forms import StyledForm
from tcms.ai_assistant.models import AIJob, AIModelConfig
from tcms.management.models import Product
from tcms.testcases.models import TestCase
from .ai_generation import check_access, draft_errors, import_drafts, inputs, submit_generation
from .models import WebAIRequest, WebAIDraft
from .views import write_guard


class GenerationForm(StyledForm, forms.Form):
    submission_token = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    title = forms.CharField(label="本次生成主题", max_length=200)
    model_config = forms.ModelChoiceField(label="生成模型", queryset=AIModelConfig.objects.none())
    documentation = forms.CharField(label="页面与功能资料", max_length=30000, widget=forms.Textarea,
        help_text="填写页面路径、元素定位器（如 #username）、操作流程和预期提示。真实账号密码请换成变量占位符。")
    requirements = forms.CharField(label="业务要求与测试重点", max_length=12000, required=False, widget=forms.Textarea,
        help_text="例如必填规则、边界范围、异常提示；资料不足会列出待确认问题，不会自动执行。")
    environment_variables = forms.CharField(label="可用环境参数名", max_length=2000, required=False,
        help_text="只填名称，英文逗号分隔，如 test_username,test_password。实际值在 Web 执行环境中设置。")
    count = forms.IntegerField(label="最多生成条数", min_value=1, max_value=10, initial=3)
    target_case = forms.ModelChoiceField(label="关联业务用例（可选）", required=False, queryset=TestCase.objects.none(), help_text="复核入库后作为此业务用例的新增 Web 脚本，不覆盖测试设计。")

    def clean_environment_variables(self):
        names = [name.strip() for name in self.cleaned_data["environment_variables"].split(",") if name.strip()]
        if len(names) > 50 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", name) for name in names):
            raise forms.ValidationError("最多 50 个变量名，只能使用字母、数字和下划线，以字母或下划线开头。")
        return sorted(set(names))

    def __init__(self, *args, owner, product=None, **kwargs):
        super().__init__(*args, **kwargs)
        from tcms.ai_assistant.scenario_permissions import editable_scenarios
        self.fields["target_case"].queryset = editable_scenarios(owner, product)
        self.fields["model_config"].queryset = AIModelConfig.objects.filter(owner=owner)
        active = self.fields["model_config"].queryset.filter(is_active=True).first()
        if active:
            self.initial.setdefault("model_config", active.pk)
        self.style_fields()
        self.fields["documentation"].widget.attrs["rows"] = 10


class ReviewForm(StyledForm, forms.ModelForm):
    revision = forms.IntegerField(widget=forms.HiddenInput)
    confirmed = forms.BooleanField(label="我已核对页面资料、定位器和断言，并解决待确认问题")

    class Meta:
        model = WebAIDraft
        fields = ("name", "description", "evidence", "steps", "review_notes")
        labels = {"name": "脚本名称"}
        help_texts = {"evidence": "从本次页面资料或业务要求中摘录 8～1000 字符原文。",
            "steps": "每条脚本至少包含一个断言，不支持任意脚本。",
            "review_notes": "存在待确认问题时，请写明确认结果。"}
        widgets = {"steps": forms.Textarea, "description": forms.Textarea, "evidence": forms.Textarea, "review_notes": forms.Textarea}

    def __init__(self, *args, context, **kwargs):
        super().__init__(*args, **kwargs)
        self.context = context
        self.initial["revision"] = self.instance.revision
        for key, limit in (("description", 4000), ("evidence", 1000), ("review_notes", 4000)):
            self.fields[key].max_length = limit
        self.style_fields()
        self.fields["steps"].widget.attrs["rows"] = 18

    def clean(self):
        data = super().clean()
        if self.instance.questions and not (data.get("review_notes") or "").strip():
            self.add_error("review_notes", "请记录待确认问题的解决结果。")
        if not self.errors:
            candidate = WebAIDraft(steps=data["steps"], evidence=data["evidence"])
            for error in draft_errors(candidate, self.context):
                self.add_error(None, error)
        return data


def private(response):
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@write_guard
def generate(request, product_id):
    product = get_object_or_404(Product, pk=product_id)
    initial = {}
    if request.GET.get("test_case"):
        from tcms.ai_assistant.scenario_permissions import editable_scenarios
        target = get_object_or_404(editable_scenarios(request.user, product), pk=request.GET["test_case"])
        initial = {"target_case":target.pk, "title":target.summary[:200], "requirements":(target.text or "")[:12000]}
    form = GenerationForm(request.POST if request.method == "POST" else None, owner=request.user, product=product, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            batch = submit_generation(request.user, product, form.cleaned_data)
        except (ValueError, RuntimeError) as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("web_testing:ai_detail", pk=batch.pk)
    batches = list(WebAIRequest.objects.filter(owner=request.user, product=product)[:20])
    jobs = AIJob.objects.filter(owner=request.user, operation="web_case_generation",
        dedupe_key__in=[f"web-generation:{batch.pk}" for batch in batches]).order_by("created")
    latest = {job.dedupe_key: job for job in jobs}
    for batch in batches:
        job = latest.get(f"web-generation:{batch.pk}")
        batch.status_label = "已生成" if batch.generated else job.get_status_display() if job else "任务记录缺失"
    return private(render(request, "web_testing/ai_generate.html", dict(form=form, product=product,
        batches=batches, title="AI 生成 Web 脚本", has_models=form.fields["model_config"].queryset.exists())))


@login_required
def detail(request, pk):
    batch = get_object_or_404(WebAIRequest.objects.select_related("product"), pk=pk, owner=request.user)
    source = inputs(batch)
    drafts = list(batch.drafts.all())
    for draft in drafts:
        draft.validation_errors = draft_errors(draft, source)
        draft.display_steps = json.dumps(draft.steps, ensure_ascii=False, indent=2)
    job = AIJob.objects.filter(owner=request.user, operation="web_case_generation", dedupe_key=f"web-generation:{batch.pk}").first()
    return private(render(request, "web_testing/ai_drafts.html", dict(batch=batch, product=batch.product,
        drafts=drafts, job=job, source=source, title=batch.title, hide_page_header=True)))


@login_required
@write_guard
def review(request, pk):
    draft = get_object_or_404(WebAIDraft.objects.select_related("request", "request__product"), pk=pk, request__owner=request.user)
    if draft.imported_at:
        return redirect("web_testing:ai_detail", pk=draft.request_id)
    source = inputs(draft.request)
    form = ReviewForm(request.POST if request.method == "POST" else None, instance=draft, context=source)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                owner = get_user_model().objects.select_for_update().get(pk=request.user.pk)
                batch = WebAIRequest.objects.select_for_update().get(pk=draft.request_id, owner=owner)
                check_access(batch, owner)
                locked = WebAIDraft.objects.select_for_update().get(pk=pk)
                if locked.imported_at or locked.revision != form.cleaned_data["revision"]:
                    raise ValueError("草稿已被其他页面修改或导入，请刷新后重试。")
                saved = form.save(commit=False)
                saved.reviewed_at, saved.revision = timezone.now(), locked.revision + 1
                saved.save(update_fields=("name", "description", "evidence", "steps", "review_notes", "reviewed_at", "revision"))
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("web_testing:ai_detail", pk=draft.request_id)
    from .validation import editor_schema
    return private(render(request, "web_testing/ai_review.html", dict(form=form, draft=draft, step_schema=editor_schema(),
        source=source, product=draft.request.product, title="编辑与复核 Web 草稿")))


@login_required
@require_POST
@write_guard
def import_selected(request, pk):
    get_object_or_404(WebAIRequest, pk=pk, owner=request.user)
    try:
        selected = [int(value) for value in request.POST.getlist("draft_ids")]
        count = import_drafts(request.user, pk, selected)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"已保存 {count} 条 Web 脚本，尚未执行浏览器测试。可在自动化脚本中继续编辑。")
    return redirect("web_testing:ai_detail", pk=pk)
