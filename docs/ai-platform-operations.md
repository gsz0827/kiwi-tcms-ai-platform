# AI 平台部署与验证

## 首次启动

1. 复制 `.env.example` 为 `.env`，填写三个独立的随机密钥/密码。`KIWI_SECRET_KEY` 至少 50 个字符，其他两项为数据库账号密码。
2. 执行 `docker compose -f docker-compose.ai.yml up --build -d`。
3. 用 `docker compose -f docker-compose.ai.yml ps -a` 检查状态：`db` 和 `web` 应健康，`migrate` 应退出且返回码为 0，`worker` 应运行。
4. 执行下面的命令完成站点域名、权限和管理员初始化。命令会交互式询问管理员账号密码：

   ```bash
   docker compose -f docker-compose.ai.yml exec -e KIWI_TENANTS_DOMAIN=localhost web \
     python manage.py initial_setup
   ```

   这一步不能只运行 `createsuperuser`：如果没有创建站点记录，首页会被重定向到 `/init-db/`。
5. 打开 `https://localhost:9443/`。浏览器需要接受本地自签名证书。若 Docker 在 Linux 虚拟机或 WSL 中运行，而浏览器在虚拟机外，将 `.env` 的 `KIWI_BIND_ADDRESS` 改为 `0.0.0.0`，并把访问地址（含 `https://`）加入 `KIWI_CSRF_TRUSTED_ORIGINS`，重建 Web 容器后使用虚拟机 IP 加 `:9443` 访问。创建产品和分类后，在 `/ai/models/` 配置个人模型，再提交需求。

Web 与 Worker 来自同一份源码镜像，使用同一个数据库和 SECRET_KEY。数据库默认不发布宿主机端口。模型服务运行在宿主机时，容器中的 `localhost` 指向容器自身，需要使用容器能够访问的模型服务地址。

## 运行时维护 AI 规则包

登录后从左侧“项目配置 → AI 规则包”进入，或打开 `/ai/instruction-profiles/`。每个项目（Kiwi 中对应一个 Product）绑定一个规则包，在包内集中维护登录、支付、权限、接口等测试规范。

首页和业务页面左侧统一提供一个“项目配置”入口，进入独立导航页后可选择“新建产品”和“维护 AI 规则包”。创建产品要求 `management.add_product` 权限；产品创建后 Kiwi 会自动生成默认用例分类、`unspecified` 版本和构建。

产品分类（上游 `Classification`）不作为平台概念：新建产品表单里没有这个字段，保存时自动归入已有分类，一个分类都没有时自动创建“默认分类”（`ai_assistant.views._default_classification`）。这样做的原因是平台里没有任何页面按分类筛选或分组，一个分类一个产品的形态下它只会多出一次选择。分类模型本身保留，系统后台（`/admin/`）与 XML-RPC 的 `Classification.*` 接口未受影响；如果将来确实要按分类组织项目，需要统一改造的入口是：项目切换器（`_project_switcher.html`，按分类分 `optgroup`）、用例库与共享目录的产品下拉、以及项目配置页。补充一条实测结论：`Product.classification` 是必需的 CASCADE 外键，删分类会级联删产品；而接口自动化的 `APIEnvironment` / `APICase` / `APIRun` / `APISuite` / `APIAIRequest` 对 `Product` 用的是 `PROTECT`，所以直接 `Classification.objects.all().delete()` 会抛 `ProtectedError`，必须先按依赖顺序清掉这些表。也就是说“彻底删掉分类”要动上游模型、表单、系统后台与 XML-RPC 四个面，收益为零——保留模型、只从界面撤退是顺的走法。

规则包在新需求提交时被保存为任务快照。管理员之后修改或停用规则包，不会改变已经排队或正在执行的任务；新的需求任务会读取最新的启用版本。每次修改内容或适用项目都会自动递增版本号，需求记录中保留使用过的规则内容，便于复盘。

规则包只用于补充原始需求，提示词约定原始需求优先。不要把 API 密钥、用户密码或其他敏感信息写入规则包。对于 JSON 格式、安全检查和权限校验等硬约束，仍然保留在代码和解析器中。

查看日志：

```bash
docker compose -f docker-compose.ai.yml logs --tail=100 migrate web worker
```

如果首次构建时 Debian 包下载很慢，可在 `.env` 中将 `DEBIAN_MIRROR` 设为 `https://mirrors.ustc.edu.cn` 后重试。默认使用 Debian 官方源。

`migrate` 失败时 Web 和 Worker 不会启动。先解决迁移日志中的错误。不要对已有业务数据库运行清库或删除数据卷命令。

## 自动化验证

不调用真实模型，在内存 SQLite 测试数据库中运行 AI 模块测试：

```bash
docker compose -f docker-compose.ai.yml --profile test run --build --rm --no-deps tests
```

该命令不读写业务数据库，模型调用由测试替身模拟。SQLite 不支持这里使用的行锁，因此并发提交测试会明确跳过。验证并发时使用以下独立 MariaDB 实例：

```bash
docker compose -f docker-compose.ai.yml --profile test-mariadb run --build --rm tests-mariadb
docker compose -f docker-compose.ai.yml --profile test-mariadb stop test-db
```

`test-db` 不发布端口，数据只保存在临时内存文件系统中，停止后丢弃；使用明确标注的测试密码，与业务数据库分开。并发测试使用两个独立数据库连接同时提交同一份表单，验证只创建一组需求、版本和任务。

浏览器脚本回归验证（需要 Node.js）：

```bash
cd tests/ai_assistant
npm ci
npx playwright install chromium
npm test
```

它在启用内容安全策略的最小测试页面中加载项目的真实脚本，检查两个按钮的提交参数、任务轮询和文本渲染。它不替代完整平台的登录、权限和真实模型联调。已有兼容 Chromium 时可通过 `PLAYWRIGHT_CHROMIUM_EXECUTABLE` 指定浏览器路径。

回归重点：

| 场景 | 预期 |
| --- | --- |
| 同一表单连续提交两次 | 一个需求、一个初始版本、一个任务 |
| 原任务已完成，再重放同一提交 | 返回原任务，不重复调用模型 |
| 同一提交标识更换内容或动作 | 显示错误，不创建新记录 |
| 打开新表单，主动提交相同内容 | 可以新建需求 |
| 两个账号使用相同标识 | 分别创建各自的数据 |
| 入队失败 | 需求和版本一同回滚 |
| Worker 启动时其他任务正在执行 | 其他任务状态保持不变 |
| 断电 / 强杀后重启 Worker | 心跳过期的任务变为「中断」，排队与已结束任务不变，不会自动重放 |
| 分析按钮和生成按钮 | 分别提交分析任务和生成任务 |
| 任务详情页 | 在内容安全策略启用时仍能轮询状态 |

上面两条 `docker compose ... run` 命令都带 `--build`，不要省略：`tests` 与 `tests-mariadb` 在 Compose 里固定引用 `kiwi-tcms-ai:test` 镜像，而 `run` 默认不会重建镜像，省略后会跑在旧镜像的代码上。等价且更省心的写法是 `make ai-test`（内部固定先 build 再 run）。

`make ai-test` 只跑 `tcms.ai_assistant`，因为 Compose 里 `tests-mariadb` 的命令写死了这个应用名。平台改动过 `tcms/core`、`tcms/testcases`、`tcms/urls.py` 等上游代码，只跑应用测试覆盖不到，因此发版前还要跑一次全量套件：

```bash
make ai-test-full
```

上游测试依赖 `parameterized` 与 `tcms-api`，由 `requirements/ai-test.txt` 装进测试镜像，缺失时相关模块会以 `ModuleNotFoundError` 整体报错而不是真正运行。

全量套件曾经报一批唯一键重复（`Duplicate entry 'AI 测试经理' for key 'name'`，SQLite 下是 `UNIQUE constraint failed: auth_user.username`）。根因已经定位：不是平台代码写错，而是「`TransactionTestCase` 清库会重新执行 `post_migrate`」与上游 `serialized_rollback` 快照叠加出来的。

1. `TransactionTestCase` 每个用例结束都会 `flush` 清库。Django 只对带 `serialized_rollback = True` 的类抑制 `post_migrate`，普通事务型用例的 flush 会照常触发它。
2. `post_migrate` 于是重跑 `create_contenttypes`、`create_permissions`、guardian 的 `create_anonymous_user`，以及本平台的 `ensure_role_groups()`。这些逻辑按名字查找、查不到就以**新的自增主键**重建，库里于是出现「名字相同、主键与迁移快照不同」的组、权限、内容类型与匿名用户。
3. RPC 用例（`LiveServerTestCase` + `serialized_rollback = True`）在 `setUpClass` 里把迁移快照反序列化回库。`deserialize_db_from_string()` 逐行**先按主键 UPDATE、落空才 INSERT**，主键一变就撞自然唯一键。反序列化按 `INSTALLED_APPS` 顺序进行，`django.contrib.auth` 里 Group 排在 User 之前，所以先撞的是本平台的角色组名「AI 测试经理」；早期先撞 `auth_user.username` 的 `AnonymousUser` 是同一个原因。

修复在 `tcms/rpc/tests/utils.py`：新增 `empty_database_before_snapshot_restore()`，`APITestCase` 与 `APIPermissionsTestCase` 的 `_fixture_setup()` 在恢复快照之前先 `flush(..., inhibit_post_migrate=True)` 清掉残留行。稳态下这一步几乎不做事（上一个用例的 teardown 已经把库清空），它只是让快照恢复不依赖「别的用例是否清干净」。回归用例是 `tcms/rpc/tests/test_serialized_rollback.py`，按类名首字母顺序跑三步：`ASnapshotFingerprintTests`（RPC 用例）记录快照恢复后的库指纹（匿名用户主键、内容类型数量与最大主键、权限数量）；`BPlainTransactionalTestCase`（普通事务用例）自己什么都不做，只靠 teardown 那次 flush 制造残留行；`CRestoreAfterPlainTransactionalTests` 再次恢复快照并断言指纹与 A 完全一致——修复前 C 会在 `setUpClass` 阶段直接抛 `IntegrityError`。

验证结果：

| 验证方式 | 结果 |
| --- | --- |
| `python manage.py test tcms.rpc.tests` 单独运行（SQLite） | 358 个用例全部通过 |
| `make ai-test-full`（全量套件同进程，含 RPC） | 通过 |

`make ai-test-full` 里显式写了 `tcms` 这个标签，这是必需的：仓库根目录的 `kiwi_lint` 是 pylint 插件包，不带标签时 unittest 会把每个包都 import 一遍去找 `load_tests`，而测试镜像里没有装 pylint/astroid，于是会多出一条与用例无关的收集错误（`ModuleNotFoundError: No module named 'astroid'`）。

全量套件必须在测试镜像里跑，不要用 `-v 仓库:/Kiwi` 挂载工作树图快。镜像里的 `/Kiwi/static`（`collectstatic` 产物）和 `tcms/locale/*/LC_MESSAGES/*.mo`（编译后的翻译）都是**构建期产物，仓库里没有对应文件**；挂载会把这些目录一并遮掉，凡是渲染整页的用例都会以 `RuntimeError: Static file "patternfly/dist/css/patternfly.min.css" does not exist and will cause 404 errors!`（抛自 `tcms/tests/storage.py`）报错——`tcms.ai_assistant` 的 288 个用例里会红 85 个，看起来像大面积回归，其实是环境问题。挂载只适合跑不渲染模板的小模块（`tcms.rpc.tests`、`tcms.bugs.tests`）做快速定位，验收一律 `make ai-test-full`。

## 导航：侧边栏与上游菜单的关系

本平台用中文侧边栏替代上游顶部的横向菜单。侧边栏自身的结构（分区、条目、图标、选中态判据）定义在 `tcms/ai_assistant/templatetags/ai_navigation.py` 的 `NAV_SECTIONS` 与 `platform_navigation` 模板标签里，`include/platform_navigation.html` 只负责循环渲染。**新增或移动菜单项请改 `NAV_SECTIONS`，不要在模板里写高亮判断**：早期版本把 `/case/ in path` 这类判据在模板里复制了三份（`is-current` / `aria-expanded` / `collapse in`），新增页面必然漏掉其中一处，于是「展开了 A 却高亮 B」成了常态。

当前分区与归属：

| 分区 | 条目 |
| --- | --- |
| 需求与设计 | 需求与任务单 `ai_assistant:index`（页面内两个 Tab：需求分析 / 任务单 `ai_assistant:dev_task_list`）、用例库 `ai_assistant:case_library` |
| 测试执行 | 测试计划 `plans-search`、执行任务 `testruns-search`、接口自动化 `ai_assistant:api_home`、后台任务 `ai_assistant:job_list` |
| 质量与缺陷 | 质量看板 `ai_assistant:dashboard`、测试报告 `ai_assistant:iteration_reports`、质量趋势 `ai_assistant:report_trends`、缺陷列表 `bugs-search`、缺陷与回归 `ai_assistant:dashboard#defect-management`、门禁设置 `ai_assistant:release_gate_settings`（**只对 `ai_assistant.approve_aireport` 可见**） |
| 度量分析 | 测试分布 `testing-breakdown`、执行总览 `execution-dashboard`、状态矩阵 `testing-status-matrix`、执行趋势 `testing-execution-trends`、用例健康度 `test-case-health` |
| 平台管理 | 项目配置 `ai_assistant:project_settings`、AI 模型配置 `ai_assistant:model_settings`、AI 调用记录 `ai_assistant:usage_logs`、AI 规则包 `ai_assistant:instruction_profiles`、成员与角色 `ai_assistant:member_list`（**只对 `ai_assistant.manage_members` 可见**） |

`SETTINGS.MENU_ITEMS`（上游顶部横向菜单的定义）**不再投影到界面上**。它有两个渲染者：页面级对象菜单用的是 `OBJECT_MENU_ITEMS`，与这里无关；真正读 `MENU_ITEMS` 的只有下面的插件收集逻辑。历史上它的条目曾被整体搬进侧边栏的一个「上游功能」分组，结果是导航里出现两套语汇（中文主菜单 + 英文分组标题）、四层缩进和 `○ / ▪` 列表符号。现在改为按语义逐个收编：

- 有价值的入口直接进上表的分区（度量类页面归入「度量分析」），或进右上角账户下拉——`用户管理` / `用户组` / `系统后台` 三项都在 `navbar.html` 的账户下拉里，它们是系统级管理而非日常工作流，且本就都通向 Django admin（`admin-users-router` 302 到 `/admin/auth/user/`，`admin-groups-router` 302 到 `/admin/auth/group/`）。三项分别用 `perms.auth.view_user` / `perms.auth.view_group` / `user.is_staff` 控制显隐；
- `测试计划` 放在「测试执行」而不是「需求与设计」：上游的「计划 → 执行」是一对，执行任务（`TestRun`）必须属于某个测试计划，用例与计划是多对多（`tcms/testcases/models.py` 的 `TestCase.plan` 走 `TestCasePlan` 中间表）。两者拆到两个分组会让这条因果链断开，看起来像互不相干的两个功能。**注意平台的应用代码不会替你建计划**：`case_library.attach_case()` 与 `create_case()` 建用例时都不挂计划，AI 生成的执行任务也只认已有的计划，所以计划要自己建——入口在测试计划搜索页标题旁的「新建测试计划」按钮；
- 与侧边栏已有入口重复的快捷方式（新建测试用例 / 搜索测试用例）直接删除。`testcases-search` 也不给入口——AI 用例库（`ai_assistant:case_library`）本来就是上游 `TestCase` 按产品切片的视图（`tcms/ai_assistant/case_library.py` 的 `visible_cases()` 用 `get_objects_for_user(..., "testcases.view_testcase")`），再列一个跨产品搜索只会制造两条通往同一模型的入口；
- 上游那组「新建 XXX」快捷方式不属于侧边栏，而是**各自列表页标题旁的按钮**（见下一条）。
- 剩下无法预知、只能运行时收集的是**插件**：`tcms/settings/common.py` 约定 `MENU_ITEMS` 的最后一项（标签为 `MORE`）固定留给 `kiwitcms.plugins` 的 entry point 追加，`plugin_menu_entries()` 读这一项，在「平台管理」末尾加一个「插件」小标题后渲染。插件自己分出来的顶层子菜单保留为 `kiwi-nav-caption` 小标题，更深的层级一律拍平成叶子——侧边栏只有 232px 宽，再缩进就没法看了。没有安装任何插件时它返回空列表，界面上不会出现多余分组。

维护时注意三点：

- 侧边栏只在已登录时渲染，检查插件菜单是否出现必须以登录态访问，匿名请求只会拿到登录页；
- 判断插件分组依赖「`MENU_ITEMS` 最后一项」这个位置约定。升级上游版本后请比对 `tcms/settings/common.py` 的 `MENU_ITEMS`，确认新增的入口已经在 `NAV_SECTIONS` 里找到位置——上游加了入口而这里没跟上时，界面上会静默少一条；
- `platform_navigation` 对 `reverse()` 抛 `NoReverseMatch` 的条目是跳过而不是报错，所以「某条菜单没出现」的第一嫌疑是 URL 名拼错，而不是权限；上表里的名字可以直接拿去比对。

### 列表页的新建入口

上游把「新建测试计划 / 新建执行任务 / 新建缺陷」放在顶部横向菜单里，侧边栏取代它之后这三个入口整组丢了——搜索页只有筛选表单和结果表，`plans-new` / `testruns-new` / `bugs-new` 三个 URL 全仓没有任何模板引用，只能手敲地址。补法是**在各自搜索页的 `{% block contents %}` 顶部加一行「页面标题 + 权限门禁按钮」**，不塞进侧边栏：

| 页面模板 | 按钮 | 目标 URL 名 | 显隐判据 |
| --- | --- | --- | --- |
| `tcms/testplans/templates/testplans/search.html` | 新建测试计划 | `plans-new` | `perms.testplans.add_testplan` |
| `tcms/testruns/templates/testruns/search.html` | 新建执行任务 | `testruns-new` | `perms.testruns.add_testrun` |
| `tcms/bugs/templates/bugs/search.html` | 新建缺陷 | `bugs-new` | `perms.bugs.add_bug` |

三条约定：按钮文案用平台口径（「新建执行任务」而不是上游译文「新的测试执行」，两个搜索页标题也补成「测试计划」「执行任务」）；URL 名不带命名空间；`testcases/search.html` **刻意不加**——AI 用例库已经有「新建测试用例」，而平台那条创建 URL 需要 product id，从上游搜索页挂一条无状态链接会造出第二条易混淆的创建路径。回归测试在 `tcms/ai_assistant/test_create_entries.py`（只授 view 权限的账号看不到按钮，只授 view 权限也能打开搜索页）。

## 导航：顶部栏（右上角）

顶部栏由 `include/navbar.html` 渲染，除品牌标识与页面级对象菜单外只有四块：项目切换器、时钟、帮助下拉、账户下拉。

- **语言下拉（地球图标）已删除**。上游那个下拉并不切换语言，五个条目分别是 Kiwi 文档、Crowdin 项目页、GitHub 新语言申请，以及只对翻译贡献者有用的「翻译模式」。本平台的语言由个人资料决定，`translation-mode` 的 URL 与视图仍然保留；将来若要多语言入口，应该做成真正的语言切换，而不是把外链加回来。
- **帮助下拉收窄**。`SETTINGS.HELP_MENU_ITEMS` 已在 `tcms/settings/ai.py` 里覆盖，只留 User Guide / Administration Guide / API Help 三条文档链接。上游默认的「Report an Issue」「Ask for help on StackOverflow」「Donate €5 via Open Collective」是社区向的，对内网使用者没有意义——他们不是 Kiwi 的用户，出问题也不该去找上游。
- **账户下拉**分四段：快捷入口（我的测试执行 / 我的测试计划）、账户与偏好（个人资料 / 修改密码 / 重置邮箱）、系统管理（用户管理 / 用户组 / 系统后台）、退出登录。
- 上游的第三方广告位 `include/ads.html`（EthicalAds）已从 `base.html` 移除。本部署的 `ANONYMOUS_ANALYTICS` 在 `tcms/settings/ai.py` 里是 `False`，Plausible 与 Scarf 像素本就不输出，那个广告条是唯一还在向外部发请求的脚本。

## 需求与任务单

需求分析回答「要做什么、有什么风险」，任务单回答「按什么顺序动手、改哪儿、怎么算做完」。两者是同一条链路的上下游，所以共用一个侧边栏入口（`ai_assistant:index`，标签「需求与任务单」），页面内用 `ai_assistant/include/workspace_tabs.html` 的两个 Tab 切换：需求分析（`ai_assistant:index`）与任务单（`ai_assistant:dev_task_list`）。

- 任务单落在 `AIDevTask`（`tcms/ai_assistant/models.py`）里，**不是草稿**：它由 AI 拆分后直接生成正式记录，之后由人在页面上改状态和内容，因此字段是结构化的（编号 / 标题 / 涉及模块 / 开发说明 / 验收标准 / 优先级 / 预估工时 / 状态）。
- 状态只有四档：`todo` 待开始、`doing` 开发中、`done` 已完成、`blocked` 阻塞。完成度 = `done / 总数`，显示在任务单页顶部、每个需求的标题旁、需求分析列表的标签里，以及全链路追踪页的第四个汇总卡片。
- 生成走后台作业：`dev_task_breakdown` 操作（`AIJob.OPERATION_CHOICES` 与 `AIUsageLog.OPERATION_CHOICES` 都要有这一项），执行器是 `jobs._execute_dev_task_breakdown`，服务函数是 `services.break_down_dev_tasks`，解析函数 `services.parse_dev_tasks` 限制 3~15 条、优先级只在 P1~P5、工时必须是正整数，编号缺省 `DEV-001` 递增。入口是任务单页底部「尚未拆分开发任务的需求」表格里的按钮（POST 到 `ai_assistant:generate_dev_tasks`，没有拆分权限时按钮不渲染）。
- 任务单可以指派给产品成员（`assignee` / `assigned_by` / `assigned_at`），指派入口在任务单行的「负责人」列；负责人只能改状态，标题、工时、负责人本身由需求提出人或测试经理维护。谁能指派见「角色与权限」一节。
- **一个需求只拆一次**：已有任务单时作业直接抛 `RuntimeError("该需求已经拆分过开发任务，请先删除现有任务单再重新拆分")`。这是沿用 `_execute_test_case_generation` 对测试用例草稿的处理方式——静默覆盖会丢掉人已经改过的状态和验收标准。要重拆就先在任务单页删掉旧任务单。
- 规则包也支持这个新任务：`AIInstructionProfile.OPERATION_CHOICES` 增加了「拆分开发任务」。**新增 operation 时必须同时改 `services._empty_skill_snapshot()`**（它返回 `requirement_analysis` / `test_case_generation` / `dev_task_breakdown` 三个空桶），否则 `capture_instruction_snapshot` 里 `{profile.operation: []}` 会因为字典没有这个键直接 KeyError。

## 角色与权限

平台里有两套互相独立的东西，别混起来看：

| 维度 | 承载方式 | 决定什么 | 在哪儿维护 |
| --- | --- | --- | --- |
| 角色 | Django 用户组（`tcms/ai_assistant/roles.py` 的 `ROLE_GROUPS`） | 能做什么动作：拆分任务单、指派、审批报告、管理成员 | 「平台管理 › 成员与角色」，或 Django admin 的用户组 |
| 产品成员 | django-guardian 的对象权限 `management.view_product` | 能看谁的东西：同一产品的成员互相可见需求、任务单、报告、缺陷 | 同一页的「添加成员 / 移出产品」 |

四个角色与能力矩阵（`roles.ROLE_PERMISSION_MATRIX` 是唯一事实来源）：

| 角色 | 权限 |
| --- | --- |
| AI 测试经理 | `split_devtask`、`assign_devtask`、`approve_aireport`、`manage_members`、`manage_requirement` + 上游 `testcases.add_testcase` / `change_testcase` |
| AI 测试工程师 | 上游 `testcases.add_testcase` / `change_testcase`（**刻意不给 `split_devtask`**：他拆的是自己提的需求，靠下面的「本人后路」通过） |
| AI 开发 | 无权限。可以看所在产品的需求与任务单，更新指派给自己的任务单状态 |
| AI 只读 | 无权限。只读，不能提交需求、不能拆分 |

两条必须先理解的原则：

- **本人后路**：凡是由本人创建的需求 / 任务单 / 报告，创建者永远对自己的东西有权限，与角色无关。所以就算一个账号没有任何角色，他提的需求自己也能拆、能编辑、能删——单人使用不会被这套权限体系锁在门外；而「AI 只读」是唯一一个连自己提的需求都不给写的角色（`roles.is_read_only`）。没角色的账号**不是**只读，这是有意的。
- **个人资产不参与角色判定**：`AIModelConfig`（含 API Key）、`AIUsageLog`、`AIJob`、接口自动化的环境与凭据永远只属于创建者本人，不随角色或产品成员关系放开。发布门禁规则**曾经**在这条清单里（挂 `owner` 的账号默认规则），现已改为产品级配置，见「发布门禁」一节——那是这一节唯一的例外，也是最容易记反的一条。

维护须知：

- 角色组与权限**不用数据迁移**：`tcms/ai_assistant/apps.py` 把 `roles.create_role_groups` 挂在 `post_migrate` 上，每次 `migrate` 结束都会按矩阵重新对齐一遍（幂等）。想立刻对齐也可以手动跑 `python manage.py setup_ai_roles`。**矩阵是唯一事实来源：在 Django admin 里手工给角色组加的权限，下次 migrate 会被覆盖掉**，要改就改 `roles.ROLE_PERMISSION_MATRIX`。
- 数据可见范围统一走 `roles.visible_*` 查询集（`visible_requests` / `visible_dev_tasks` / `visible_analyses` / `visible_reports` / `visible_defects` / `visible_verifications` / `visible_iterations` / `visible_reviews`），视图里不要再写 `filter(owner=request.user)`。缺陷草稿的产品路径是 `execution__run__plan__product`——`TestExecution` 指向执行任务的外键叫 `run` 而不是 `test_run`，写错会直接 FieldError。
- **列表看得到的，详情也必须打得开**：列表页用 `visible_*` 放行之后，凡是列表里点得进去的详情页与动作页都要用同一个查询集取对象，否则会出现「列表里看得到、点开 403/404」的分裂（造演示数据时经理在「测试质量趋势」点开同事的报告就是 403）。已经对齐的入口：报告详情 `views.edit_report`、执行任务的报告列表 `views.run_report`、测试质量趋势 `views.report_trends`、迭代报告详情 `views.iteration_report_detail`、草稿用例编辑 `views.edit_draft` 与导入 `views.import_request`、回归验证 `views.create_regression_verification`；回归测试见 `tcms/ai_assistant/test_shared_visibility.py`。`views._has_run_permission` 里额外认「同一产品的成员」，就是为了让上游 `testruns.view_testrun` 的对象级校验与这套可见范围一致。**例外（一致地按作者隔离，不要顺手改）**：缺陷草稿（`execution_defect` / `edit_defect_draft` / `link_defect_draft` / `sync_defect_status`）与用例评审（`review_case` / `apply_review`）——这两条路的列表与详情都只看本人，`visible_defects` / `visible_reviews` 目前只用于看板指标。
- 能力判定统一走 `roles.can_*` 函数；模板里用 `roles.can_*` 的结果（视图传进上下文的 `can_edit` / `can_split` / `can_assign` / `can_manage`）决定按钮是否渲染。侧边栏入口的可见性由 `ai_navigation` 的 `perm` 字段控制（`_user_has_perm`），没权限的入口直接不渲染，避免点进去只拿到 403。
- guardian 的坑：`get_users_with_perms(obj, only_with_perms_in=...)` **只认 codename**（`view_product`），传全名 `management.view_product` 不会报错、只会静默返回空集合。所以 `roles` 里同时存了 `MEMBERSHIP_PERMISSION`（全名，给 `assign_perm` / `get_objects_for_user` 用）与 `MEMBERSHIP_CODENAME`（给 `only_with_perms_in` 用）。
- 任务单的指派人由 `AIDevTask.assignee` / `assigned_by` / `assigned_at` 记录；候选人限定为该产品的成员（`roles.assignable_users`），非成员会被拒绝。负责人只能改状态（表单在 `can_manage=False` 时只保留 `status` 字段），全面编辑留给需求提出人与测试经理。

## 共享目录

「共享目录」是给人一种方式，把散在各个列表页里的资源收进自己定义的目录树。数据落在两张表里（`tcms/ai_assistant/models.py`）：`ProjectResourceFolder`（项目共享资源目录，自引用 `parent` 成树）与 `ProjectResourceAssignment`（资源归档到哪个目录，唯一键是 `(resource_type, object_id)`，所以一条资源最多在一个目录里）。资源类型由 `ProjectResourceFolder.RESOURCE_TYPES` 定义，现有 `requirement` / `case` / `plan` 三种。

界面不是页面各自实现的：`tcms/templates/base.html` 调用 `{% platform_resource_browser %}`, 标签返回非空时自动把 `{% block contents %}` 包成「左侧 310px 目录栏 + 右侧正文」的两栏布局。标签本体在 `tcms/ai_assistant/templatetags/ai_navigation.py`，按 `resolver_match.url_name` 分流，**给新页面接线就是在 `platform_resource_browser` 里加一个分支**（目录树、未归档分组、管理共享目录弹窗、每条资源的「移动到目录」下拉、详情弹窗都在 `include/platform_resource_browser.html` 与 `include/platform_resource_item.html` 里共用）。

目前接线的页面：

| 页面 | `url_name` | 资源 | 目录栏范围 |
| --- | --- | --- | --- |
| AI 需求分析 | `ai_assistant:index` | 需求 | `roles.visible_requests`（本产品成员互相可见） |
| AI 用例库 | `ai_assistant:case_library` | 用例 | 当前产品的目录，且跟随页面筛选 |
| 搜索测试用例（上游） | `testcases-search` | 用例 | 全量用例，`?product=` 时按产品过滤 |
| 搜索测试计划（上游） | `plans-search` | 计划 | 全量计划，`?product=` 时按产品过滤 |

用例库这一条有两个专门的处理，改动时别丢掉：

- **目录栏与表格共用一套范围**：两者都走 `case_library.library_cases(request, product)`，因此 `?type=manual|automated`、`?category=`、`?q=` 同时作用于表格与目录栏。如果目录栏改回「取该产品的全部用例」，被筛掉的用例会从目录里冒出来，`test_api_scheduling` 与 `test_shared_folders` 里的断言会直接失败。
- **目录只列当前产品**：`_build_resource_browser(folder_product=...)` 会同时把目录查询限定在该产品，并把产品 id 放进返回值 `default_product_id`，供「新建目录」弹窗预选（`platform_resource_browser.js` 按产品过滤上级目录选项）。

权限分两件事，别混：

- **管理目录**（新建 / 重命名 / 删除）走 `views._require_folder_management_permission`：`requirement` 对所有登录用户开放——需求目录是团队共同整理的公共设施，跟角色无关；`case` / `plan` 分别需要上游 `testcases.change_testcase` / `testplans.change_testplan`。没有权限时目录栏里连「管理共享目录」按钮都不渲染（`browser.can_manage`）。
- **把资源放进目录**走 `views._resource_for_assignment`：需求用 `roles.visible_requests`（产品成员可以互相归档对方的需求），用例 / 计划需要对应的 change 权限，并且**目录必须与资源同属一个产品、同一种资源类型**，否则 403。资源本身没有权限改动，目录只是视图层的归类。

另外两条行为约束：删除目录不会删资源（里面的资源退回「未归档」），表单 POST 的 `next` 只在以单个 `/` 开头时才用于跳转。回归测试在 `tcms/ai_assistant/test_shared_folders.py`。

## 发布门禁

发布门禁回答一个问题：**这份测试报告能不能放行**。判定函数是 `tcms/ai_assistant/engineering.py` 的 `evaluate_release_gate(product, metrics_snapshot, test_run_ids)`，四项检查都取自报告那一刻的执行指标：不存在未关闭的阻断级缺陷、未关闭缺陷数不超过上限、成功率不低于阈值、所有用例都已执行。

规则挂在**产品**上，不挂在账号上。`AIReleaseGateRule` 每个产品最多一条（`Meta.constraints` 的 `unique_ai_gate_rule` 唯一约束，保存即更新同一行），字段有 `block_priority` / `max_open_defects` / `min_success_rate` / `require_all_executed` / `is_active` / `updated_by`。没配规则的产品回落内置默认：**P1 缺陷为 0、未关闭缺陷为 0、成功率至少 95%、全部执行**（`engineering.BUILTIN_GATE`，判定结果里的 `rule` 是「内置默认门禁」、`rule_scope` 是 `builtin`，界面上会直接把这句话说出来）。

历史上这套东西「有形无实」，原因有四个，改的时候别退回去：

| 曾经的形态 | 现在 |
| --- | --- |
| 规则挂 `owner`，视图只查 `owner=request.user` | 挂 `product`，全产品统一生效 |
| 缺陷口径按 `owner=登记人` 过滤，同事登记的 P1 不计入 → 假通过 | 只按 `execution__run_id__in` 过滤，谁登记的缺陷都算 |
| 判定完不拦任何动作，`release_decision` 是编辑表单里的自由下拉 | 门禁未过时禁止「审批通过」，发布结论由门禁推导、不可手改 |
| 用没用到内置默认门禁，界面上看不出来 | 报告页与设置页都标注「内置默认门禁」 |

**硬门禁与风险放行**（`views.approve_report`）：每次审批都现场重算门禁，不复用可能已经过期的 `report.gate_result`。门禁未通过时，普通账号的「审批通过」会被拦下并提示去修复阻断项；只有测试经理（`roles.can_manage_release_gate`，即拥有 `ai_assistant.approve_aireport`）能填 `waive_reason` 做**风险放行**，放行人、放行时间、放行理由写进 `gate_waived_by` / `gate_waived_at` / `gate_waive_reason`，并在 `AITestReportRevision` 里留一条 `change_reason` 以「风险放行：」开头的修订记录；报告页和导出 HTML 都会带上这段留痕。未放行时会清空上一轮的放行字段，所以放行是一次性的、可以撤销。

发布结论不再是人填的，而是由门禁推导（`engineering.derived_release_decision(gate_result, approval_status, waived=False)`）：

| 门禁 | 审批状态 | 发布结论 |
| --- | --- | --- |
| 通过 | 已批准 | 可以发布 |
| 通过 | 待审批 / 已驳回 | 有条件发布 |
| 未通过 | 已批准且已风险放行 | 可以发布 |
| 未通过 | 其他 | 不建议发布 |

因此 `AIReportForm` 的 `Meta.fields` 里**没有** `release_decision`（谁都不能靠编辑表单改它），报告生成作业（`jobs.py`）写报告时也是用这个函数推导结论，LLM 的措辞只进 `conclusion` / `summary`。迭代报告同样按门禁推导。

维护须知：

- 门禁设置页 `ai_assistant:release_gate_settings` 按 `roles.can_manage_release_gate` 判断（视图直接 `PermissionDenied`），侧边栏那一条也带 `"perm": "ai_assistant.approve_aireport"`，没权限就整条不渲染，不会点进去只拿 403；
- 这个角色判定**有意不留「本人后路」**：报告作者不能给自己的报告放行，否则硬门禁等于没有。单人部署下管理员本来就是 superuser / staff，仍然进得去；
- 设置页列出**所有产品**（`?product=<pk>` 可预填表单），没规则的产品显示「尚未配置，正在使用内置默认门禁」并给一个配置链接；
- 更新已有规则必须把那条记录交给表单：`AIReleaseGateRuleForm(request.POST, instance=existing)`。`AIReleaseGateRuleForm` 是 ModelForm，唯一性校验（`unique_ai_gate_rule`）会把「这个产品已经有规则」判成重复——传 `instance=None` 时第二次保存会静默变成一个表单错误，界面上只看到「已存在」，规则却怎么也改不动。`test_release_gate.py` 的 `test_saving_a_rule_twice_updates_the_same_row` 就是钉这个的；
- 迁移 `0026_remove_aireleasegaterule_unique_ai_gate_rule_and_more.py` 把 `product` 从可空改成必填，并在最前面插了一段 `clean_gate_rules`：先删 `product` 为空的旧规则，再按 `-updated` / `-pk` 对同产品去重（不去重唯一约束建不起来）。以后再做类似的「放宽列 → 收紧列」改动，同样要先清数据再改结构；
- 回归测试在 `tcms/ai_assistant/test_release_gate.py`（内置默认、作者被拦、经理需理由、放行留痕、门禁通过可直接批、结论不可手改、设置页权限、重复保存是更新同一行、侧边栏显隐）。

## 界面文案与术语约定

AI 模型配置页的名字同时出现在侧边栏、页面标题、表格表头和多处拦截提示里，这些位置必须用同一个词，否则同一个概念会长出好几个名字。当前约定：

| 位置 | 用词 |
| --- | --- |
| 侧边栏「平台管理 › AI 模型配置」、页面标题 | AI 模型配置 |
| 表单字段标签 | 配置名称 / 服务地址（Base URL）/ API Key / 模型 ID / 请求超时（秒）/ 设为默认模型 |
| 当前生效的那条配置 | 「默认模型」，未生效的按钮为「设为默认」 |

配套两条规则：

- 模型相关的拦截提示统一写成「请先配置一个 AI 模型并设为默认，再……」，不要退回「配置并启用」；`views.py`、`services.py`、`jobs.py` 与三个页面模板里都有这类文案，改词时要一起改。
- `api_key` 的必填性由 `AIModelConfigForm.__init__` 按「该配置是否已存密钥」决定，必填提示语必须通过字段的 `error_messages` 覆盖，否则界面上会落回 Django 默认的「这个字段是必填项。」。

`PersonalAIModelConfigTests.test_model_form_labels_and_hints_are_unified` 锁定上述用词与字段顺序，改动文案时它会失败。

注意 `AIModelConfig` 的 `verbose_name`（`models.py` 里仍是「API 地址」「模型名称」「超时时间（秒）」）**尚未**跟进：该模型未注册 admin，表单标签由 `Meta.labels` 覆盖，所以目前不影响任何界面，改它需要额外生成一个迁移。

## 健康检查与日志

平台提供两个语义不同的探针，注意不要混用：

| 路径 | 语义 | 判定依据 |
| --- | --- | --- |
| `/health/` | 存活 | 进程能响应 HTTP 即为 200，**不访问数据库** |
| `/ready/` | 就绪 | 数据库可连通且迁移全部已应用才为 200，否则 503 |

`/health/` 刻意不碰数据库，也不在 `CheckDBStructureExistsMiddleware` 的拦截范围内。因此数据库断开时 `/health/` 仍返回 200，而 `/ready/` 返回 503 并附带原因——两者一对比就能确定故障层次，不会再把数据库故障误报成进程故障。`/ready/` 的响应体会列出 `database`、`migrations`、`cache` 三项明细；`cache` 标注为 `critical: false`，它异常只上报、不影响整体判定。

`/ready/` 的数据库探测跑在带超时的独立线程上，默认 3 秒（`KIWI_READINESS_TIMEOUT`）。这不是过度设计，而是实测逼出来的：停止数据库容器后，Docker 内置 DNS 解析主机名要 8 秒才报错，驱动层加上重试后单次连接阻塞 32 秒，而 uwsgi 的 `harakiri` 是 30 秒——结果不是返回 503，而是 worker 被 `SIGKILL`、nginx 回 502，并且被卡住的 worker 会连带让整个站点不可用。加了这个上限之后，同一场景下 `/health/` 8.1 秒降到 0.01 秒、`/ready/` 由 502 变为 3 秒内返回 503。

同时给数据库连接建立了上限（`KIWI_DB_CONNECT_TIMEOUT`，默认 5 秒）：同一场景下驱动层的阻塞时间从 32 秒降到 8 秒。该值必须明显小于 uwsgi 的 `harakiri = 30`。

`web` 服务的容器健康检查使用 `/ready/`，所以 `docker compose -f docker-compose.ai.yml ps` 显示 `unhealthy` 意味着服务不可用，而不只是进程退出。镜像自身的 `HEALTHCHECK` 使用 `/health/`。随时可以手工确认：

```bash
make ai-health
```

每行容器日志都带 `request_id`，访问日志形如 `method=GET path=/ai/ status=200 duration=0.031s`。响应头会回写 `X-Request-ID`；如果反向代理传入了合法的 `X-Request-ID`，平台会复用它，从而把入口链路与后端日志串起来。日志级别通过 `.env` 的 `KIWI_LOG_LEVEL` 调整（默认 `INFO`）。

## 中断任务恢复

Worker 领取任务后会持续刷新任务的**心跳**（`AIJob.heartbeat`，接口自动化执行写在 `APIRun.heartbeat`），间隔 20 秒（`tcms/ai_assistant/leases.py` 的 `HEARTBEAT_INTERVAL_SECONDS`）。执行进程断电、被 `docker kill`、OOM 或宿主机重启之后心跳就停了：超过 **600 秒**（`AI_JOB_LEASE_SECONDS` / `API_RUN_LEASE_SECONDS`）没有心跳，就判定执行进程已经不在了。

判定由 Worker 自己完成，不需要人工介入：

- **启动时巡检一次**，之后空闲时每 60 秒（`SWEEP_INTERVAL_SECONDS`）巡检一次（`--sweep-interval 0` 可关掉周期巡检，`--lease-seconds` 可临时改判租约）。
- 失去心跳的 AI 任务从 `running` 变为**中断**（`interrupted`），`cancel_requested` 变为已取消；接口自动化执行同样变中断，并把仍为「未执行」的结果标成「已跳过」。
- **绝不自动重放。** 模型调用和接口请求可能已经产生副作用，所以巡检只把状态说清楚，是否重来由人在页面上决定：任务详情页对中断任务显示「重试」按钮，重试会新建一个任务（`attempts + 1`）。
- 其他 Worker 正在处理的任务心跳是新鲜的，不会被误判；排队（`queued`）和已结束的任务永远不动。

人工兜底仍然保留，用于 Worker 停着、或需要立刻处理而不想等租约超时的场合：

```bash
# 巡检：默认只预览，不修改任何数据
docker compose -f docker-compose.ai.yml run --rm --no-deps worker \
  python manage.py ai_recover_jobs --stale

# 确认 Worker 已全部停止后落地（把过期任务标成中断）
docker compose -f docker-compose.ai.yml run --rm --no-deps worker \
  python manage.py ai_recover_jobs --stale --apply --workers-stopped

# 按指定任务 ID 恢复（旧用法，仍然可用；这条路径标成「失败」而不是「中断」）
docker compose -f docker-compose.ai.yml run --rm --no-deps worker \
  python manage.py ai_recover_jobs 任务UUID --apply --workers-stopped

# 接口自动化执行
docker compose -f docker-compose.ai.yml run --rm --no-deps worker \
  python manage.py api_recover_runs --stale --workers-stopped
```

`--workers-stopped` 是管理员确认，不是自动检测；带 `--apply` 时必须给出。所有恢复路径都不会回滚已经保存的业务结果，也不会自动重试——核对结果之后，再在页面上决定是否重试，避免重复产生报告或补充用例。

正常停止（`docker compose stop worker`、SIGTERM）时 Worker 会完成当前任务再退出，不领取新任务，Compose 最多等待 11 分钟后强制停止；被强制停止的任务留下的心跳会在租约到期后由下一次巡检标成中断。

## 升级

先备份数据库、附件和 `.env`。停止 Web 和 Worker 后构建并执行迁移，避免旧代码与新数据结构同时运行：

```bash
docker compose -f docker-compose.ai.yml stop web worker
docker compose -f docker-compose.ai.yml build
docker compose -f docker-compose.ai.yml run --rm migrate
docker compose -f docker-compose.ai.yml up -d
```

确认迁移成功后再执行最后一步。运行中的任务若被停止，按上面的恢复步骤处理。不要随意更换 SECRET_KEY，否则已保存的模型密钥无法解密。
