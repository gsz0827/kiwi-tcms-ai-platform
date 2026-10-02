from django.urls import path
from . import views

app_name = "web_testing"
urlpatterns = [
    path("environments/", views.environments, name="environments"),
    path("environments/new/", views.edit_environment, name="environment_new"),
    path("environments/<int:pk>/", views.edit_environment, name="environment_edit"),
    path("cases/", views.cases, name="cases"),
    path("cases/new/", views.edit_case, name="case_new"),
    path("cases/<int:pk>/", views.edit_case, name="case_edit"),
    path("cases/<int:pk>/delete/", views.delete_case, name="case_delete"),
    path("suites/", views.suites, name="suites"),
    path("suites/new/", views.edit_suite, name="suite_new"),
    path("suites/<int:pk>/", views.edit_suite, name="suite_edit"),
    path("suites/<int:pk>/delete/", views.delete_suite, name="suite_delete"),
    path("suites/<int:pk>/execute/", views.submit, name="submit"),
    path("runs/", views.runs, name="runs"),
    path("runs/<uuid:pk>/", views.run_detail, name="run"),
    path("runs/<uuid:pk>/status/", views.status, name="status"),
    path("runs/<uuid:pk>/cancel/", views.cancel, name="cancel"),
    path("runs/<uuid:pk>/retry/", views.retry, name="retry"),
    path("screenshots/<int:pk>/", views.screenshot, name="screenshot"),
]
