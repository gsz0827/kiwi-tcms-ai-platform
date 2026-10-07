# 前端布局与导航审查报告

> 审查时间：2026-10-02
> 审查范围：侧边栏导航结构、菜单跳转、选中态、顶部导航、页面标题与文案、品牌残留

## 一、总体结论

导航结构本身设计良好：侧边栏用 `NAV_SECTIONS`（`tcms/ai_assistant/templatetags/ai_navigation.py`）集中定义，
所有菜单项引用的 URL name 均已验证存在（`core-views-index`、`bugs-search`、`testruns-search`、
`testcases-search`、`plans-search`、`testing-breakdown` 等 telemetry 五连均真实），
所有 render 模板文件均存在，**没有"菜单跳转到不存在的页面"这类致命问题**。

发现的问题集中在三类：**品牌残留、文案与行为不符、选中态边缘 bug**。

## 二、问题清单

### P1（建议修复，影响用户体验/品牌一致性）

| # | 位置 | 问题 | 修复建议 |
|---|------|------|----------|
| 1 | `tcms/templates/base.html:17` | 页面 `<title>` 仍是上游 `Kiwi TCMS - the leading open source test case management system`，未改为平台自己的名称 | 改为「AI 测试管理平台」 |
| 2 | `tcms/templates/navbar.html:16` | 顶部 logo 仍是 `kiwi_h20.png`（Kiwi 猕猴桃品牌图），alt/title 是 `DASHBOARD` | 换成平台自己的 logo，或至少改 alt/title |
| 3 | `tcms/ai_assistant/templates/ai_assistant/dashboard.html`（第 8 行附近） | 说明文案「仅汇总**当前账号创建**的 AI 需求、分析、缺陷草稿…」与实际不符：`views.dashboard` 用的是 `roles.visible_requests/visible_analyses/visible_reports/visible_defects`，实际是**同一产品成员互相可见** | 文案改为「汇总你所在产品的需求、分析、缺陷草稿、报告与回归记录」 |
| 4 | 侧边栏顶部「工作台」（`core-views-index`） | 「工作台」指向上游 `core_views.DashboardView`（`dashboard.html`），是 Kiwi 原生通用看板，与平台自己的「质量看板」（`ai_assistant:dashboard`）定位重复、容易混淆 | 二选一：把「工作台」改为跳转 `ai_assistant:dashboard`；或保留上游工作台但改名「系统首页」与「质量看板」区分 |

### P2（边缘 bug，影响小但值得修）

| # | 位置 | 问题 | 修复建议 |
|---|------|------|----------|
| 5 | `ai_navigation.py:478-479` | `platform_navigation` 里这段 `if any(current in candidate.get("names", ()) ...)` 会**覆盖 `_item_is_active` 的 fragments 匹配**：当某页 url_name 命中任一菜单的 names 时，其它靠 fragments 匹配的菜单高亮会被强制关掉 | 这段逻辑疑似冗余/有副作用，需结合具体页面回归验证后精简 |
| 6 | `ai_navigation.py:482-483` | API 导航 tab 选中态：`request.GET.get("tab", "environments")` 当 tab 为非法值（如 `tab=xxx`）时，`api_views.home` 会重置为 `environments`，但导航判断 `item.get("query") == "tab=xxx"` 全不匹配 → **四个 tab 都不高亮** | 非法 tab 时按 `environments` 兜底判断，或在 home 里统一规范化 tab |
| 7 | `web_testing/navigation.py:14` | Web「测试套件」菜单把 `environments`（测试环境页）的 names 也归入「测试套件」高亮，但侧边栏里**没有独立的「测试环境」入口**，用户进环境页时高亮落在「测试套件」上，语义略错位 | 可考虑为「测试环境」单独加一个菜单项，或保持现状但确认是设计意图 |

### P3（信息备注，非缺陷）

- 侧边栏「缺陷列表」指向上游 `bugs-search`（Kiwi 原生缺陷搜索），「缺陷与回归」指向 `dashboard#defect-management`（AI 缺陷草稿）。两套缺陷体系并存是设计选择，但命名相近（「缺陷列表」vs「缺陷与回归」），用户容易混淆哪个是 AI 缺陷、哪个是上游缺陷。
- 顶部导航 `navbar.html` 的 Help 菜单仍链接到 `github.com/kiwitcms/Kiwi` 的 CHANGELOG（第 58 行），对平台用户指向错误仓库。

## 三、已核验无问题的项

- ✅ 所有侧边栏菜单的 URL name 均能 reverse（含 telemetry 五个度量页、上游 bugs/testruns/testcases/plans search）
- ✅ 所有 `render()` 引用的模板文件均存在，无 TemplateDoesNotExist 隐患
- ✅ 「缺陷与回归」锚点 `#defect-management` 在 dashboard.html 第 112 行存在
- ✅ 接口自动化四个 tab（environments/cases/suites/runs）与 `api_views.home` 的合法值一致
- ✅ 侧边栏「需求管理」的 names 完整覆盖需求相关全部子页面
- ✅ 产品切换器、时钟、用户菜单等顶部组件逻辑正常
