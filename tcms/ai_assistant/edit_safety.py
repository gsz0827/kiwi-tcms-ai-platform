"""Signed optimistic baselines, checked while holding the row through the save."""

from functools import wraps
import hashlib
import json

from django.core import signing
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction


def fingerprint(value):
    encoded = json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def edit_state(instance):
    if instance._meta.model_name == "airequest":
        names = (
            "title",
            "requirement",
            "document_sections",
            "category_id",
            "version",
            "target_version_id",
            "assigned_to_id",
            "priority",
            "status",
        )
    else:
        names = [field.attname for field in instance._meta.concrete_fields]
    return {name: getattr(instance, name) for name in names}


def edit_token(instance, user):
    return signing.dumps(
        {
            "kind": instance._meta.label_lower,
            "pk": instance.pk,
            "user": user.pk,
            "hash": fingerprint(edit_state(instance)),
        },
        salt="document-edit-v1",
        compress=True,
    )


def guard_edit(model):
    def decorate(view):
        @wraps(view)
        def protected(request, *args, **kwargs):
            pk = kwargs.get("pk")
            if pk is None:
                return view(request, *args, **kwargs)
            with transaction.atomic():
                query = model.objects.all()
                if request.method == "POST" and model._meta.model_name != "aidevtask":
                    query = query.select_for_update()
                instance = query.filter(pk=pk).first()
                if instance is None:
                    return view(request, *args, **kwargs)
                if request.method == "POST" and instance._meta.model_name == "aidevtask":
                    # Requirement edits/rechecks lock parent first, then child rows.
                    from .models import AIRequest

                    parents = set(instance.case_designs.values_list("request_id", flat=True)) | {
                        instance.request_id
                    }
                    list(AIRequest.objects.select_for_update().filter(pk__in=parents).order_by("pk"))
                    instance = model.objects.select_for_update().get(pk=pk)
                current = edit_token(instance, request.user)
                request.document_edit_token = (
                    request.POST.get("edit_token", "") if request.method == "POST" else current
                )
                request.document_edit_conflict = False
                if request.method == "POST":
                    try:
                        supplied = signing.loads(
                            request.document_edit_token, salt="document-edit-v1", max_age=86400
                        )
                        expected = signing.loads(current, salt="document-edit-v1")
                        request.document_edit_conflict = supplied != expected
                    except (signing.BadSignature, TypeError, ValueError):
                        request.document_edit_conflict = True
                # The original view still checks visibility and edit permissions.
                return view(request, *args, **kwargs)

        return protected

    return decorate


def valid_edit(request, form):
    if getattr(request, "document_edit_conflict", False):
        form.add_error(
            None,
            "内容已变更或编辑页面已过期，未保存本次修改。请保留当前输入，在新页面打开最新内容后核对。",
        )
        return False
    return True
