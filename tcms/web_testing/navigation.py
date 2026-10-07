"""Navigation by planning, shared assets, and specialised automation workflows."""
TEST_NAV_SECTIONS = (
    {"key": "requirements", "label": "需求与开发任务", "icon": "fa-lightbulb-o", "items": (
        {"label": "需求管理", "icon": "fa-lightbulb-o", "url": "ai_assistant:index", "names": (
            "ai_assistant:index", "ai_assistant:case_design", "ai_assistant:edit_requirement", "ai_assistant:requirement_trace", "ai_assistant:edit_draft", "ai_assistant:review_case", "ai_assistant:apply_review", "ai_assistant:generate_from_analysis", "ai_assistant:analyze_coverage", "ai_assistant:supplement_from_coverage", "ai_assistant:import",
        )},
        {"label": "开发任务", "icon": "fa-tasks", "url": "ai_assistant:dev_task_list", "names": (
            "ai_assistant:dev_task_list", "ai_assistant:dev_task_detail", "ai_assistant:dev_task_create", "ai_assistant:edit_dev_task", "ai_assistant:delete_dev_task", "ai_assistant:generate_dev_tasks",
        )},
    )},
    {"key": "testing", "label": "测试管理", "icon": "fa-list-alt", "items": (
        {"label": "用例库", "icon": "fa-list", "url": "ai_assistant:scenario_library", "names": ("ai_assistant:scenario_library", "ai_assistant:scenario_detail", "ai_assistant:scenario_new", "ai_assistant:scenario_edit", "ai_assistant:case_hub", "ai_assistant:case_library", "ai_assistant:library_case_new"), "fragments": (("/case/", None), ("/cases/", "/api-testing/"))},
        {"label": "测试计划", "icon": "fa-calendar", "url": "plans-search", "fragments": (("/plan/", None), ("/plans/", None))},
        {"label": "执行任务", "icon": "fa-play", "url": "testruns-search", "names": ("ai_assistant:run_analysis",), "fragments": (("/runs/", None), ("/run/", None))},
    )},
    {"key": "web", "label": "Web 自动化测试", "icon": "fa-desktop", "items": (
        {"label": "测试环境", "icon": "fa-server", "url": "web_testing:environments", "names": ("web_testing:environments", "web_testing:environment_new", "web_testing:environment_edit")},
        {"label": "自动化脚本", "icon": "fa-list", "url": "web_testing:cases", "names": ("web_testing:cases", "web_testing:case_new", "web_testing:case_edit", "web_testing:case_debug", "web_testing:ai_generate", "web_testing:ai_detail", "web_testing:ai_review", "web_testing:ai_import")},
        {"label": "测试套件", "icon": "fa-cubes", "url": "web_testing:suites", "names": ("web_testing:suites", "web_testing:suite_new", "web_testing:suite_edit")},
        {"label": "执行任务", "icon": "fa-play", "url": "web_testing:runs", "names": ("web_testing:runs", "web_testing:submit", "web_testing:run", "web_testing:status", "web_testing:cancel", "web_testing:retry", "web_testing:screenshot", "web_testing:regression_new", "web_testing:regression_verify")},
    )},
    {"key": "api", "label": "接口自动化测试", "icon": "fa-exchange", "items": (
        {"label": "测试环境", "icon": "fa-server", "url": "ai_assistant:api_home", "query": "tab=environments", "names": ("ai_assistant:api_home", "ai_assistant:api_environment_new", "ai_assistant:api_environment_edit")},
        {"label": "自动化脚本", "icon": "fa-list", "url": "ai_assistant:api_home", "query": "tab=cases", "names": ("ai_assistant:api_case_new", "ai_assistant:api_case_edit", "ai_assistant:api_ai_generate", "ai_assistant:api_ai_detail", "ai_assistant:api_ai_review", "ai_assistant:postman_upload", "ai_assistant:postman_preview")},
        {"label": "测试套件", "icon": "fa-cubes", "url": "ai_assistant:api_home", "query": "tab=suites", "names": ("ai_assistant:api_suite_new", "ai_assistant:api_suite_edit", "ai_assistant:api_suite", "ai_assistant:api_suite_action")},
        {"label": "执行任务", "icon": "fa-play", "url": "ai_assistant:api_home", "query": "tab=runs", "names": ("ai_assistant:api_submit", "ai_assistant:api_report", "ai_assistant:api_rerun", "ai_assistant:api_cancel", "ai_assistant:api_export")},
    )},
)
