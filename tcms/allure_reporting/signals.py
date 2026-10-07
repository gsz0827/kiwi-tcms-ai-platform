from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver
from tcms.web_testing.models import WebRun
from tcms.ai_assistant.models import APIRun
from .models import AllureReport


@receiver(post_save, sender=WebRun, dispatch_uid="allure_new_web_run")
@receiver(post_save, sender=APIRun, dispatch_uid="allure_new_api_run")
def queue_report(sender, instance, created, raw=False, **kwargs):
    if created and not raw and getattr(settings, "ALLURE_ENABLED", True):
        # Created within the source transaction: rollback cannot orphan a report.
        relation = {"web_run" if sender is WebRun else "api_run": instance}
        AllureReport.objects.get_or_create(**relation, defaults={"owner": instance.owner})
