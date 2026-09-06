from django.urls import path

from . import views


app_name = "ai_assistant"


urlpatterns = [
    path("", views.index, name="index"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path(
        "project-context/",
        views.set_project_context,
        name="set_project_context",
    ),
    path(
        "resource-folders/create/",
        views.create_resource_folder,
        name="create_resource_folder",
    ),
    path(
        "resource-folders/<int:pk>/rename/",
        views.rename_resource_folder,
        name="rename_resource_folder",
    ),
    path(
        "resource-folders/<int:pk>/delete/",
        views.delete_resource_folder,
        name="delete_resource_folder",
    ),
    path(
        "resource-folders/assign/",
        views.assign_resource_folder,
        name="assign_resource_folder",
    ),
    path("reports/trends/", views.report_trends, name="report_trends"),
    path("reports/iterations/", views.iteration_reports, name="iteration_reports"),
    path(
        "reports/iterations/<int:pk>/",
        views.iteration_report_detail,
        name="iteration_report_detail",
    ),
    path("release-gates/", views.release_gate_settings, name="release_gate_settings"),
    path("jobs/", views.job_list, name="job_list"),
    path("jobs/<uuid:pk>/", views.job_detail, name="job_detail"),
    path("jobs/<uuid:pk>/status/", views.job_status, name="job_status"),
    path("jobs/<uuid:pk>/cancel/", views.cancel_job, name="cancel_job"),
    path("jobs/<uuid:pk>/retry/", views.retry_job, name="retry_job"),
    path(
        "requests/<int:pk>/generate/",
        views.generate_from_analysis,
        name="generate_from_analysis",
    ),
    path(
        "requests/<int:pk>/coverage/",
        views.analyze_coverage,
        name="analyze_coverage",
    ),
    path(
        "requests/<int:pk>/coverage/supplement/",
        views.supplement_from_coverage,
        name="supplement_from_coverage",
    ),
    path("requests/<int:pk>/import/", views.import_request, name="import"),
    path("requests/<int:pk>/edit/", views.edit_requirement, name="edit_requirement"),
    path("requests/<int:pk>/trace/", views.requirement_trace, name="requirement_trace"),
    path("drafts/<int:pk>/edit/", views.edit_draft, name="edit_draft"),
    path("reviews/case/<int:pk>/", views.review_case, name="review_case"),
    path("reviews/<int:pk>/apply/", views.apply_review, name="apply_review"),
    path("models/", views.model_settings, name="model_settings"),
    path("usage/", views.usage_logs, name="usage_logs"),
    path("runs/<int:pk>/analysis/", views.run_analysis, name="run_analysis"),
    path("runs/<int:pk>/report/", views.run_report, name="run_report"),
    path(
        "executions/<int:pk>/defect/",
        views.execution_defect,
        name="execution_defect",
    ),
    path(
        "defects/<int:pk>/edit/",
        views.edit_defect_draft,
        name="edit_defect_draft",
    ),
    path(
        "defects/<int:pk>/link/",
        views.link_defect_draft,
        name="link_defect_draft",
    ),
    path(
        "defects/<int:pk>/sync/",
        views.sync_defect_status,
        name="sync_defect_status",
    ),
    path(
        "defects/<int:pk>/regression/",
        views.create_defect_regression,
        name="create_defect_regression",
    ),
    path("reports/<int:pk>/edit/", views.edit_report, name="edit_report"),
    path("reports/<int:pk>/approve/", views.approve_report, name="approve_report"),
    path("reports/<int:pk>/gate/", views.evaluate_report_gate, name="evaluate_report_gate"),
    path("reports/<int:pk>/export/html/", views.export_report_html, name="export_report_html"),
    path("reports/<int:pk>/export/pdf/", views.export_report_pdf, name="export_report_pdf"),
    path(
        "reports/<int:pk>/regression/",
        views.create_regression_verification,
        name="create_regression_verification",
    ),
    path(
        "models/<int:pk>/edit/",
        views.edit_model_config,
        name="edit_model_config",
    ),
    path(
        "models/<int:pk>/activate/",
        views.activate_model_config,
        name="activate_model_config",
    ),
    path(
        "models/<int:pk>/test/",
        views.test_model_config,
        name="test_model_config",
    ),
]
