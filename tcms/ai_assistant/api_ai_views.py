import json
import re
import uuid
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from guardian.shortcuts import get_objects_for_user

from tcms.management.models import Product
from tcms.testcases.models import Category, TestCase
from .api_ai import check_access, draft_errors, import_drafts, inputs, submit_generation, validate_configuration
from .api_forms import StyledForm
from .automation_ui import write_guard
from .models import APIAIRequest, APIAIDraft, AIJob, AIModelConfig


class GenerationForm(StyledForm, forms.Form):
    submission_token = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    title = forms.CharField(label="本次生成主题", max_length=200)
    category = forms.ModelChoiceField(label="用例业务分类", queryset=Category.objects.none())
    target_case = forms.ModelChoiceField(label="关联已有测试用例（可选）", required=False, queryset=TestCase.objects.none(),
        help_text="选择后保存为该用例的新增接口脚本；原步骤和已有脚本不会被覆盖。留空则创建新测试用例。")
    model_config = forms.ModelChoiceField(label="生成模型", queryset=AIModelConfig.objects.none())
    documentation = forms.CharField(label="接口文档", max_length=30000, widget=forms.Textarea,
        help_text="粘贴接口方法、路径、参数、认证方式、响应示例或 OpenAPI/cURL 片段。请用变量占位符替换真实凭据。")
    requirements = forms.CharField(label="业务要求与测试重点", max_length=12000, required=False, widget=forms.Textarea,
        help_text="例如必填字段、边界范围、异常状态码；未明确的信息将列为待确认问题。")
    environment_variables = forms.CharField(label="可用环境参数名", max_length=2000, required=False,
        help_text="只填名称，以英文逗号分隔，例如 test_username,test_password,user_id；实际值在执行环境中配置。")
    count = forms.IntegerField(label="最多生成条数", min_value=1, max_value=10, initial=3)

    def __init__(self, *args, owner, product, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(product=product)
        self.fields["target_case"].queryset = get_objects_for_user(owner, "testcases.change_testcase", klass=TestCase).filter(category__product=product)
        self.fields["model_config"].queryset = AIModelConfig.objects.filter(owner=owner)
        config = self.fields["model_config"].queryset.filter(is_active=True).first()
        if config:
            self.initial.setdefault("model_config", config.pk)
        self.style_fields()
        self.fields["documentation"].widget.attrs["rows"] = 10

    def clean_environment_variables(self):
        names = [name.strip() for name in self.cleaned_data["environment_variables"].split(",") if name.strip()]
        if len(names) > 50 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", name) for name in names):
            raise forms.ValidationError("最多 50 个变量名，只能使用字母、数字和下划线，以字母或下划线开头。")
        return sorted(set(names))


class ReviewForm(StyledForm, forms.ModelForm):
    revision = forms.IntegerField(widget=forms.HiddenInput)
    confirmed = forms.BooleanField(label="我已对照接口资料复核配置，并解决列出的待确认问题")

    class Meta:
        model = APIAIDraft
        fields = ("name", "description", "evidence", "configuration", "review_notes")
        labels = {"name": "用例名称", "description": "前置条件、步骤与预期结果", "evidence": "原文依据",
                  "configuration": "请求与断言配置（JSON）", "review_notes": "确认说明"}
        help_texts = {"evidence": "从本次接口资料、业务要求或规则包中摘录 8～1000 字符的原文，便于追溯。",
                      "review_notes": "存在待确认问题时，写明确认结果或修改依据。",
                      "configuration": "只支持请求字段、断言和提取变量。空值状态码需补全；不支持脚本。"}
        widgets = {"configuration": forms.Textarea(attrs={"rows": 18}), "review_notes": forms.Textarea}

    def __init__(self, *args, context, **kwargs):
        super().__init__(*args, **kwargs)
        self.context = context
        self.initial["revision"] = self.instance.revision
        self.fields["description"].max_length = 4000
        self.fields["evidence"].max_length = 1000
        self.fields["review_notes"].max_length = 4000
        self.style_fields()
        self.fields["configuration"].widget.attrs["rows"] = 18

    def clean(self):
        data = super().clean()
        if self.instance.questions and not (data.get("review_notes") or "").strip():
            self.add_error("review_notes", "请记录待确认问题的解决结果。")
        if self.errors:
            return data
        try:
            data["configuration"] = validate_configuration(data["configuration"])
        except (ValueError, RecursionError) as exc:
            self.add_error("configuration", str(exc) if isinstance(exc, ValueError) else "配置嵌套过深。")
            return data
        candidate = APIAIDraft(configuration=data["configuration"], evidence=data["evidence"])
        for error in draft_errors(candidate, self.context):
            self.add_error(None, error)
        return data


def private(response):
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@permission_required("testcases.add_testcase", raise_exception=True)
@write_guard
def generate(request, product_id):
    product = get_object_or_404(Product, pk=product_id)
    initial = {}
    target = request.GET.get("test_case", "")
    if target.isdigit():
        case = get_object_or_404(get_objects_for_user(request.user, "testcases.change_testcase", klass=TestCase),
                                pk=target, category__product=product)
        initial = dict(target_case=case.pk, title=case.summary[:200], category=case.category_id, requirements=(case.text or "")[:12000])
    form = GenerationForm(request.POST if request.method == "POST" else None, owner=request.user, product=product, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            batch = submit_generation(request.user, product, form.cleaned_data)
        except (ValueError, RuntimeError) as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("ai_assistant:api_ai_detail", pk=batch.pk)
    batches = list(
        APIAIRequest.objects.filter(owner=request.user, product=product)
        .select_related("category")[:20]
    )
    jobs = AIJob.objects.filter(
        owner=request.user, operation="api_case_generation",
        dedupe_key__in=[f"api-generation:{row.pk}" for row in batches],
    ).order_by("created")
    latest = {job.dedupe_key: job for job in jobs}
    for batch in batches:
        job = latest.get(f"api-generation:{batch.pk}")
        if batch.generated:
            batch.status_label = "已生成"
        else:
            batch.status_label = job.get_status_display() if job else "任务记录缺失"
    return private(
        render(
            request,
            "ai_assistant/api/ai_generate.html",
            dict(form=form, product=product, batches=batches, has_models=form.fields['model_config'].queryset.exists()),
        )
    )


@login_required
def detail(request, pk):
    batch = get_object_or_404(APIAIRequest, pk=pk, owner=request.user)
    context = inputs(batch)
    drafts = list(batch.drafts.select_related("api_case").all())
    for draft in drafts:
        draft.validation_errors = draft_errors(draft, context)
        draft.display_config = json.dumps(draft.configuration, ensure_ascii=False, indent=2)
    job = AIJob.objects.filter(owner=request.user, operation="api_case_generation", dedupe_key=f"api-generation:{batch.pk}").first()
    imported_ids = [draft.api_case_id for draft in drafts if draft.api_case_id]
    execute_url = reverse("ai_assistant:api_submit", args=[batch.product_id]) + "?" + urlencode({"case": imported_ids}, doseq=True)
    return private(render(request, "ai_assistant/api/ai_drafts.html", dict(batch=batch, product=batch.product,
        drafts=drafts, job=job, source=context, rules=context["rules"].get("test_case_generation", []),
        imported_ids=imported_ids, execute_url=execute_url)))


@login_required
@permission_required("testcases.add_testcase", raise_exception=True)
@write_guard
def review(request, pk):
    draft = get_object_or_404(APIAIDraft.objects.select_related("request"), pk=pk, request__owner=request.user)
    if draft.imported_at:
        return redirect("ai_assistant:api_ai_detail", pk=draft.request_id)
    context = inputs(draft.request)
    form = ReviewForm(request.POST if request.method == "POST" else None, instance=draft, context=context)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                owner = get_user_model().objects.select_for_update().get(pk=request.user.pk)
                batch = APIAIRequest.objects.select_for_update().get(pk=draft.request_id, owner=owner)
                check_access(batch, owner)
                locked = APIAIDraft.objects.select_for_update().get(pk=pk)
                if locked.imported_at or locked.revision != form.cleaned_data["revision"]:
                    raise ValueError("草稿已被其他页面修改或导入，请刷新后重试。")
                saved = form.save(commit=False)
                saved.reviewed_at, saved.revision = timezone.now(), locked.revision + 1
                saved.save(update_fields=("name", "description", "evidence", "configuration", "review_notes", "reviewed_at", "revision"))
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("ai_assistant:api_ai_detail", pk=draft.request_id)
    return private(render(request, "ai_assistant/api/ai_review.html", dict(form=form, draft=draft, source=context, product=draft.request.product)))


@login_required
@permission_required("testcases.add_testcase", raise_exception=True)
@require_POST
@write_guard
def import_selected(request, pk):
    get_object_or_404(APIAIRequest, pk=pk, owner=request.user)
    try:
        ids = [int(value) for value in request.POST.getlist("draft_ids")]
    except ValueError:
        messages.error(request, "草稿编号无效，请重新选择。")
        return redirect("ai_assistant:api_ai_detail", pk=pk)
    try:
        import_drafts(request.user, pk, ids)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "复核后的草稿已保存到用例库，尚未发送任何测试请求。")
    return redirect("ai_assistant:api_ai_detail", pk=pk)
