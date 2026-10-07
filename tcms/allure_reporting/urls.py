from django.urls import path
from . import views

app_name = "allure_reporting"
urlpatterns = [
    path("<str:kind>/<uuid:pk>/", views.view, name="view"),
    path("<str:kind>/<uuid:pk>/status/", views.status, name="status"),
    path("<str:kind>/<uuid:pk>/generate/", views.generate, name="generate"),
    path("<str:kind>/<uuid:pk>/artifact/", views.artifact, name="artifact"),
]
