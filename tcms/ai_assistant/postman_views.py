import hashlib
import json
from datetime import timedelta
from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from tcms.management.models import Product
from .automation_ui import home_url, write_guard
from .case_directories import can_manage_common, can_manage_folder, visible_folders
from .case_library import attach_case
from .crypto import encrypt_api_key, decrypt_api_key
from .models import APICase, PostmanImport, ProjectResourceFolder, ProjectResourceAssignment
from .postman_import import MAX_BYTES, parse_collection
from .api_validation import validate_case


class UploadForm(forms.Form):
    collection = forms.FileField(label='Postman 集合', help_text='Collection v2 / v2.1 JSON，最多 2 MB。',
                                 widget=forms.FileInput(attrs={'accept': '.json,application/json', 'class': 'form-control'}))


class ConfirmForm(forms.Form):
    selected = forms.MultipleChoiceField(label='选择请求', widget=forms.CheckboxSelectMultiple)
    confirmed = forms.BooleanField(label='我已了解：仅导入请求，Postman 脚本和断言须人工重建，复核前不能执行。')

    def __init__(self, *args, rows, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['selected'].choices = [(str(row['index']), row['name']) for row in rows if not row['error']]


def import_access(user, product):
    if not can_manage_common(user, product):
        raise PermissionDenied('没有维护该项目共享目录的权限。')


@login_required
@permission_required('testcases.add_testcase', raise_exception=True)
@write_guard
@never_cache
def upload(request, product_id):
    product = get_object_or_404(Product, pk=product_id)
    import_access(request.user, product)
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        file = form.cleaned_data['collection']
        try:
            if file.size > MAX_BYTES:
                raise ValueError('集合文件不能超过 2 MB。')
            raw = file.read(MAX_BYTES + 1)
            payload = parse_collection(raw)
            digest = hashlib.sha256(raw).hexdigest()
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=request.user.pk)
                batch, _ = PostmanImport.objects.get_or_create(owner=request.user, product=product,
                    fingerprint=digest, defaults={'title': payload['title'], 'expires': timezone.now()})
                if not batch.imported:
                    batch.payload_encrypted = encrypt_api_key(json.dumps(payload, ensure_ascii=False))
                    batch.expires = timezone.now() + timedelta(minutes=20)
                    batch.save(update_fields=('payload_encrypted', 'expires'))
            return redirect('ai_assistant:postman_preview', product_id=product.pk, pk=batch.pk)
        except (ValueError, TypeError, AttributeError, RecursionError):
            form.add_error('collection', '集合格式无效或不受支持；请使用 v2 / v2.1 JSON，检查格式与大小。')
    return render(request, 'ai_assistant/api/postman_upload.html', {
        'title': '导入 Postman 集合', 'product': product, 'form': form,
        'back_url': home_url(product, 'cases')})


def create_selected(batch, selected, user):
    payload = json.loads(decrypt_api_key(batch.payload_encrypted))
    rows = [row for row in payload['rows'] if str(row['index']) in selected]
    if len(rows) != len(selected) or not rows or any(row['error'] for row in rows):
        raise ValueError('选择包含不支持的请求，请重新预览。')
    import_access(user, batch.product)
    Product.objects.select_for_update().get(pk=batch.product_id)
    folder_ids = []
    for row in rows:
        parent = None
        for name in row['folders']:
            folder = ProjectResourceFolder.objects.filter(product=batch.product, resource_type='case_group',
                                                          parent=parent, name=name).first()
            if folder is None:
                folder = ProjectResourceFolder.objects.create(product=batch.product, resource_type='case_group',
                    parent=parent, name=name, created_by=user, updated_by=user)
            if not can_manage_folder(user, folder):
                raise PermissionDenied
            parent = folder
        folder_ids.append(parent.pk)
    ids = []
    for position, (row, folder_id) in enumerate(zip(rows, folder_ids)):
        data = row['configuration']
        validate_case(data)
        script = APICase.objects.create(owner=user, product=batch.product, sequence=(position+1)*10,
                                       import_review_required=True, **data)
        case = attach_case(script)
        ProjectResourceAssignment.objects.create(folder_id=folder_id, resource_type='case',
                                                 object_id=case.pk, assigned_by=user)
        ids.append(script.pk)
    batch.case_ids, batch.imported, batch.payload_encrypted = ids, timezone.now(), ''
    batch.save(update_fields=('case_ids', 'imported', 'payload_encrypted'))
    return ids


@login_required
@permission_required('testcases.add_testcase', raise_exception=True)
@write_guard
@never_cache
def preview(request, product_id, pk):
    batch = get_object_or_404(PostmanImport.objects.select_related('product'), pk=pk,
                             product_id=product_id, owner=request.user)
    import_access(request.user, batch.product)
    if batch.imported:
        scripts = APICase.objects.filter(owner=request.user, product=batch.product, pk__in=batch.case_ids)
        return render(request, 'ai_assistant/api/postman_done.html', {
            'title': 'Postman 导入记录', 'batch': batch, 'scripts': scripts,
            'product': batch.product, 'back_url': home_url(batch.product, 'cases')})
    if batch.expires <= timezone.now():
        return HttpResponse('预览已过期，请重新上传集合。', status=410)
    payload = json.loads(decrypt_api_key(batch.payload_encrypted))
    rows = payload['rows']
    form = ConfirmForm(request.POST or None, rows=rows)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=request.user.pk)
                batch = PostmanImport.objects.select_for_update().get(pk=batch.pk, owner=request.user)
                if batch.imported:
                    return redirect(request.path)
                if batch.expires <= timezone.now():
                    raise ValueError('预览已过期，请重新上传集合。')
                ids = create_selected(batch, set(form.cleaned_data['selected']), request.user)
            messages.success(request, f'已导入 {len(ids)} 条接口脚本，请复核后执行。')
            return redirect(request.path)
        except ValueError as exc:
            form.add_error(None, str(exc))
    selected = set(request.POST.getlist('selected')) if form.is_bound else set()
    for row in rows:
        row['selected'] = str(row['index']) in selected
        row['directory'] = ' / '.join([batch.product.name] + row['folders'])
    return render(request, 'ai_assistant/api/postman_preview.html', {
        'title': '预览 Postman 集合', 'batch': batch, 'rows': rows, 'form': form,
        'product': batch.product, 'back_url': home_url(batch.product, 'cases')})
