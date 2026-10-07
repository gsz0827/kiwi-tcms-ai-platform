"""Legacy form fixtures submit the same fresh signed baseline as a rendered editor.

Concurrency/missing-token tests use django.test.Client directly and never auto-refresh.
"""

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import resolve
from urllib.parse import urlsplit
from tcms.testcases.models import TestCase
from .models import AIRequest, AIDevTask
from .edit_safety import edit_token


class EditClient(Client):
    def post(self, path, data=None, *args, **kwargs):
        match = resolve(urlsplit(path).path)
        model = {
            "ai_assistant:edit_requirement": AIRequest,
            "ai_assistant:edit_dev_task": AIDevTask,
            "ai_assistant:scenario_edit": TestCase,
        }.get(match.view_name)
        if model and isinstance(data, dict) and "edit_token" not in data:
            instance = model.objects.filter(pk=match.kwargs.get("pk")).first()
            user = get_user_model().objects.filter(pk=self.session.get("_auth_user_id")).first()
            if instance and user:
                data = dict(data, edit_token=edit_token(instance, user))
        return super().post(path, data, *args, **kwargs)
