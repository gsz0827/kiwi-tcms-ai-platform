# 测试功能路线完整性自检表

> 本文档是「AI 测试管理平台」测试功能路线的可验收自检清单，供企业级落地前逐项核对。
> 每一行标注：功能环节 → 核心代码 → 对应测试 → CI 覆盖位置 → 完整度评估。
> 生成时间：2026-10-02。以当前代码与 CI 配置为准，改动后请同步更新本文档。

## 0. 测试路线全景

```
需求分析 → 任务拆分 → 用例生成/评审 → 用例库管理 → 测试计划/执行
  → 接口自动化 + Web自动化 → 缺陷管理 → 回归验证 → 报告 → 发布门禁
       ↑ 全链路有 dashboard 闭环评分 + requirement_trace 追溯矩阵
```

## 1. 功能环节自检

| # | 环节 | 核心代码 | 对应测试 | CI 位置 | 完整度 |
|---|------|----------|----------|---------|--------|
| 1 | 需求分析 | `services.py`（analyze 流程） | `test_api_ai.py`、`tests.py` | sqlite + mariadb | ✅ 完整 |
| 2 | 任务单拆分/指派 | `services.py` + `views.generate_dev_tasks` | `tests.py` | sqlite + mariadb | ✅ 完整 |
| 3 | 用例生成/评审 | `services.py`（generate/review prompt） | `tests.py`、`test_api_ai.py` | sqlite + mariadb | ✅ 完整 |
| 4 | 覆盖分析/补充用例 | `services.py`（coverage_matrix/supplement） | `test_automation_completion.py` | sqlite + mariadb | ✅ 完整 |
| 5 | 用例库管理 | `case_library.py` | `test_create_entries.py` | sqlite + mariadb | ✅ 完整 |
| 6 | 接口自动化 | `api_runner.py`、`api_views.py`、`api_scheduling.py` | `test_api_automation.py`（567 行） | sqlite + mariadb | ✅ 完整 |
| 7 | Web 自动化（编排） | `web_testing/`（runner/models/views） | `tests.py`（161 行）+ **`test_runner.py`（本次新增）** | sqlite + mariadb | ✅ 完整 |
| 8 | Web 自动化（真实浏览器） | `web_testing/runner.execute` | **`test_e2e_browser.py`（本次新增）** | **`web-e2e.yml`（本次新增）** | ✅ 补齐 |
| 9 | 缺陷管理 | `engineering.py`、`audit.py` | `test_audit.py`（434 行）、`test_audit_regressions.py` | sqlite + mariadb | ✅ 完整 |
| 10 | 回归验证 | `engineering.verify_defect_regression` | `test_audit_regressions.py` | sqlite + mariadb | ✅ 完整 |
| 11 | 报告/门禁 | `engineering.evaluate_release_gate` | `test_release_gate.py`（261 行） | sqlite + mariadb | ✅ 完整 |
| 12 | 后台任务可靠性 | `jobs.py`、`leases.py` | `test_reliability.py`、`test_worker_lease.py` | sqlite + mariadb | ✅ 完整 |
| 13 | 度量看板/闭环评分 | `views.dashboard` | `test_health.py` | sqlite + mariadb | ✅ 完整 |
| 14 | 需求追溯矩阵 | `views.requirement_trace` | `test_shared_visibility.py` | sqlite + mariadb | ✅ 完整 |

## 2. 关键结论

1. **测试功能路线已完整闭环**，14 个环节全部有代码、有测试、有 CI 覆盖，无断点。
2. **历史短板已补齐**：Web 自动化的 `runner.execute()` 此前只有 mock 级覆盖（`claim_run`/`recover_stale`），本次新增 `test_runner.py`（7 个执行路径用例）与 `test_e2e_browser.py`（真实浏览器冒烟），并新增 `web-e2e.yml` CI job。
3. **执行路径测试的数据库约束**：`runner.execute()` 通过 `ThreadPoolExecutor` 子线程读写数据库，SQLite 的 `:memory:` 库每个线程独立连接、互不可见，因此：
   - `test_runner.py` 与 `test_e2e_browser.py` 在 **MariaDB** 环境真正运行（CI 的 `platform-tests-mariadb` 与 `web-e2e`）。
   - 在 SQLite 环境（`make ai-test` 走 Dockerfile.ai 测试镜像，未装 playwright 且用 SQLite）会自动跳过，不会误报。
4. **测试分层**：`mock 单测（SQLite 快速回归）→ mock 单测（MariaDB 全量）→ 真实浏览器 E2E（web-e2e）` 三层，覆盖从纯逻辑到真实浏览器交互。

## 3. 本次新增/改动清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `tcms/web_testing/test_runner.py` | 新增 | mock Playwright，覆盖 `execute()` 的 7 个状态机路径：成功、步骤失败、环境异常、取消、登录态复用、失败即停、非 running 空转 |
| `tcms/web_testing/test_e2e_browser.py` | 新增 | 真实 Chromium + 本地静态站点，验证通过/失败用例、失败截图、账号归属 |
| `.github/workflows/web-e2e.yml` | 新增 | 独立 CI job：起 MariaDB + 装 Chromium + 跑 `test_e2e_browser` |

## 4. 企业级落地前建议补充（超出测试功能本身的保障项）

以下不属于「测试功能」缺口，而是「把测试能力用于生产」的保障项，供参考：

- [ ] **覆盖度量化门禁**：`dashboard` 已有闭环评分，但未接入 CI 卡点；可考虑对 `coverage_score` 设阈值（如 < 某值阻断发布）。
- [ ] **接口自动化与 Web 自动化的 CI 触发打通**：`api_suite_views.ci_runs` 已支持 CI 提交，但需在部署流水线里真正挂上。
- [ ] **测试数据管理独立界面**：`automation_data.py` 的变量/多组数据已加密保存，但数据集是「套件内嵌」，无跨套件复用的独立管理页。
- [ ] **度量导出/趋势**：`report_trends` 已有趋势视图，可确认是否满足管理层的报表导出需求。
