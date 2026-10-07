import uuid
from django.conf import settings
from django.db import models


class PostmanImport(models.Model):
    """Owner-scoped, encrypted preview; fingerprint prevents accidental repeated imports."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    product = models.ForeignKey('management.Product', on_delete=models.CASCADE)
    fingerprint = models.CharField(max_length=64)
    title = models.CharField(max_length=120)
    payload_encrypted = models.TextField(blank=True)
    case_ids = models.JSONField(default=list)
    created = models.DateTimeField(auto_now_add=True)
    expires = models.DateTimeField()
    imported = models.DateTimeField(null=True)

    class Meta:
        app_label = 'ai_assistant'
        constraints = [models.UniqueConstraint(fields=('owner', 'product', 'fingerprint'),
                                               name='unique_postman_import_source')]
