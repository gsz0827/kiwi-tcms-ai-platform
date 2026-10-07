"""Suite submissions share the same immutable queue as browser submissions."""
import hashlib
import json
from .crypto import decrypt_api_key
import secrets
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from .api_runner import submit_run
from .roles import is_read_only
from .models import APICase, APIRun, APISuite


def suite_data(suite, submission_token):
    cases = list(APICase.objects.filter(
        pk__in=suite.case_ids, owner=suite.owner, product=suite.product))
    if not cases or len(cases) != len(suite.case_ids):
        raise ValueError("套件中的用例已删除或所属项目发生变化，请重新保存套件。")
    return dict(environment=suite.environment, cases=cases, submission_token=submission_token,
                stop_on_failure=suite.stop_on_failure, share_cookies=suite.share_cookies,
                test_run=None, passed_status=None, failed_status=None,
                datasets=json.loads(decrypt_api_key(suite.datasets_encrypted) or "[]"))


def queue_suite(pk, owner_id, *, trigger="manual", key=None, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        # Same lock order as submit_run and edits: owner -> suite -> environment.
        owner = get_user_model().objects.select_for_update().get(pk=owner_id)
        suite = APISuite.objects.select_for_update().get(pk=pk, owner=owner)
        if not owner.is_active or is_read_only(owner):
            raise ValueError("套件所属账号已停用或为只读账号，不能执行测试。")
        if trigger == "schedule":
            if not suite.schedule_enabled or not suite.next_run_at or suite.next_run_at > now:
                return None
            key = str(suite.next_run_at.timestamp())
        submission_token = uuid.uuid5(uuid.NAMESPACE_URL, f"kiwi-suite:{pk}:{trigger}:{key or uuid.uuid4()}")
        existing = APIRun.objects.filter(owner=owner, suite=suite, submission_token=submission_token).first()
        if existing:
            return existing
        active = suite.runs.filter(status__in=APIRun.ACTIVE_STATUSES).exists()
        if trigger == "schedule":
            # Coalesce missed ticks; never replay a backlog after downtime.
            suite.next_run_at = now + timedelta(minutes=max(5, suite.interval_minutes))
            suite.last_error = "上次执行尚未结束，本次已跳过。" if active else ""
            suite.save(update_fields=("next_run_at", "last_error"))
            if active:
                return None
        elif active:
            raise ValueError("该套件已有排队或执行中的任务，请等待完成。")
        try:
            # Savepoint keeps a failed submission from rolling back the next tick.
            with transaction.atomic():
                run = submit_run(owner, suite.product, suite_data(suite, submission_token),
                                 suite=suite, trigger=trigger)
        except (ValueError, ObjectDoesNotExist):
            if trigger != "schedule":
                raise
            suite.last_error = "提交失败：请检查环境白名单、用例、变量和项目归属后重新保存套件。"
            suite.save(update_fields=("last_error",))
            return None
        suite.last_triggered, suite.last_error = now, ""
        suite.save(update_fields=("last_triggered", "last_error"))
        return run


def dispatch_due_suites():
    now = timezone.now()
    due = list(APISuite.objects.filter(schedule_enabled=True, next_run_at__lte=now)
               .order_by("next_run_at").values_list("pk", "owner_id")[:100])
    count = 0
    for pk, owner_id in due:
        try:
            count += queue_suite(pk, owner_id, trigger="schedule", now=now) is not None
        except (ValueError, ObjectDoesNotExist):
            APISuite.objects.filter(pk=pk, owner_id=owner_id).update(
                schedule_enabled=False, last_error="账号不可用，定时执行已暂停。")
    return count


def rotate_token(suite):
    token = secrets.token_urlsafe(32)
    suite.ci_token_hash = hashlib.sha256(token.encode()).hexdigest()
    suite.ci_token_expires = timezone.now() + timedelta(days=90)
    suite.save(update_fields=("ci_token_hash", "ci_token_expires"))
    return token
