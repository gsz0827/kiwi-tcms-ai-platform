# 接口自动化使用说明

平台现在可以按产品维护 HTTP 接口用例，在后台逐条执行、检查断言、查看报告，并可选回写已有测试运行。不依赖 AI 模型，不把 AI 生成的文字直接当作可执行脚本。

## 更新并启动

在项目目录执行，演示服务只在 Docker 内部网络开放，不占宿主机端口：

```bash
docker compose --env-file .env -f docker-compose.ai.yml --profile automation-demo up --build -d
```

此命令会构建代码、运行迁移并启动 Web、Worker、Scheduler 和演示服务。浏览器强制刷新后，用例在 **需求与设计 → 测试用例**，执行环境、自动化套件及报告在 **测试执行 → 接口自动化**。

已有的 `worker` 同时处理 AI 任务和接口任务，两类队列交替领取。接口任务完全不需要配置 AI 模型。部署时 Web 和 Worker 必须使用同一版镜像。

## 第一次走通流程

1. 选择已有产品；如果没有，先在左侧“项目配置”新建产品。
2. 点击“添加演示环境与用例”，会为当前账号、当前产品添加一个环境和四条用例。重复点击不会重复创建。
3. 点击“执行接口测试”，环境选“本地接口演示”，选择四条演示用例。先不选择回写运行。
4. 提交后进入报告页面，页面自动更新。
5. 正常结果是 **3 条通过、1 条断言失败**：服务健康检查、查询用户、登录通过；最后一条故意把不存在的用户预期设为 200，实际返回 404。
6. 展开请求和响应摘要，查看失败断言的实际值和预期值。编辑最后一条用例，预期状态码改为 404，重新提交应全部通过；第一次报告仍保留原来的失败结果。

演示接口：`GET /health`、`GET /users/1`、`POST /login`。演示登录 JSON 为 `{"username":"demo","password":"demo-password"}`，返回的 token 仅为演示数据。

## 测试自己的接口

`.env` 中添加 Worker 可以访问的测试服务来源，精确包含协议、主机和端口，以英文逗号分隔：

```dotenv
KIWI_API_ALLOWED_ORIGINS=http://api-demo:8080,http://192.168.190.128:8080
```

然后使 Web 和 Worker 加载配置：

```bash
docker compose --env-file .env -f docker-compose.ai.yml up -d web worker scheduler
```

服务地址从 **Docker 中的 Worker** 访问。`localhost` 指 Worker 容器自身；虚拟机上运行的接口一般填写虚拟机 IP。HTTPS 使用正常证书校验；不提供跳过证书验证选项。执行器仅允许部署者明确开放的服务，不跟随重定向，不使用代理环境变量，不自动重试。Cookie 共享默认关闭，可按单次执行或套件开启。

新建环境时填写服务地址（可包含 `/api` 等前缀）、超时、公共请求头和普通环境变量。认证请求头单独加密保存，编辑时不回显；留空表示保留，勾选清除可以删除。`KIWI_SECRET_KEY` 同时用于解密这些配置，部署更新时保持原值。

环境变量示例：

```json
{"user_id": 1, "page_size": 20}
```

接口路径写 `/users/{{user_id}}`；查询参数写 `{"limit":"{{page_size}}"}`。请求体里的完整变量引用保留数字等 JSON 类型。变量在提交前校验，缺少变量会拒绝整批提交。

支持 GET、POST、PUT、PATCH、DELETE、HEAD。勾选“发送 JSON 请求体”才会发送 body。可通过响应变量提取传递登录令牌，已有固定令牌也可以配置在环境认证请求头中。目前不支持 multipart 文件上传和脚本。

### 登录与业务接口串联

首页点击“添加登录链路演示”，会新增三条用例，保留你已有的独立演示用例与手动修改。选择这三条一起执行，正常结果为全部通过：

| 顺序 | 接口 | 提取或引用 |
| --- | --- | --- |
| 10 | `POST /login` | 从 `token` 提取 `session_token` |
| 20 | `GET /me` | 请求头 `Authorization: Bearer {{session_token}}`，从 `data.id` 提取 `account_id` |
| 30 | `GET /users/{{account_id}}` | 使用前一步的用户 ID |

原有部署更新后，需要同时更新演示服务，才能访问新增的 `/me` 接口。使用本指南开头的启动命令即可。

每条接口用例都有“执行顺序”，数值越小越先执行，相同时按创建顺序。旧用例默认都是 100，保持原来的顺序。提交后顺序也会保存到快照中，后续编辑不会改变排队中的任务。

响应提取字段填写 JSON 对象，左边是变量名，右边是响应 JSON 路径：

```json
{"access_token": "data.token", "order_id": "data.order.id"}
```

后续用例可在路径、请求头、查询参数、JSON 请求体、断言预期值中使用 `{{变量名}}`。令牌请求头应配置在需要认证的后续用例中；不要把依赖登录结果的变量放入所有请求都会使用的环境公共头。

提取值支持字符串、数字和布尔值，单值最多 4096 字符，不支持对象、数组或 null；数组元素可用 `items.0.id` 提取。提取字段不存在或类型不支持时，报告会出现一条失败的提取检查。

只有该用例的请求断言与所有提取检查都通过，变量才会交给后续步骤。失败时会清除该步骤声明的变量，哪怕环境里原来有同名旧值；依赖这些变量的用例会标记“已跳过”并注明缺少的变量，不发送 HTTP 请求。未依赖失败步骤的独立用例仍可继续。

执行表单可勾选“遇到失败或请求异常时停止后续用例”，让整个批次在首个失败处停止。提取变量仅在本次 Worker 执行的内存中存在，不写回环境，不保存在运行配置快照中；报告保留提取检查及脱敏后的响应。下一次执行会重新登录和提取。

### 断言

每条用例始终检查 HTTP 状态码。可设置响应时间上限，0 表示不检查。JSON 字段断言示例：

```json
[
  {"path": "data.id", "operator": "equals", "expected": 1},
  {"path": "data.name", "operator": "exists"},
  {"path": "items.0.enabled", "operator": "equals", "expected": true}
]
```

支持点分隔的对象路径和数字数组索引，不执行表达式。`equals` 严格比较类型和值，`exists` 检查字段存在（字段值可以是 null）。包含点号的 JSON 键暂不支持。非 JSON 响应不能通过 JSON 字段断言。

一次最多 20 条用例，每条超时 1–30 秒，单条配置最多 64 KB，响应正文最多 1 MB。普通 HTTP 错误状态（如 404、500）照常进入断言；连接、证书、超时、超大响应等标记为“请求异常”。后续步骤按依赖关系与停止策略处理，已发送的请求不会自动重放。

## 多组测试数据与统一报告归档

执行表单及套件提供“多组测试数据”，例如 `[{"user_id":1},{"user_id":2}]`。使用既有 `{{user_id}}` 语法引用，每组覆盖环境中的同名变量。最多 10 组，数据组数 × 用例数不得超过 20。每组完整执行所选链路，组间清空 Cookie、提取变量和登录态；定时与 CI 提交也使用套件保存的数据。配置与执行快照加密保存。

多组数据不允许直接反复覆盖同一条 TCMS 测试执行。执行结束后可选择“归档报告 / 创建缺陷草稿”，归档到有权限的同产品测试计划、同版本构建。每个数据组的结果独立保留，环境异常或跳过项计入未完成，不计通过；只有勾选的失败项会创建待确认缺陷草稿。归档入口不会调用 AI 或自动批准报告，同一执行来源最多归档一次。

报告快照不包含请求参数、登录凭据、响应正文或 Web 截图，仅共享名称、状态、耗时及来源链接。后续可在原有报告页面继续审批、导出、门禁和迭代汇总，在缺陷草稿页面关联已有缺陷及发起原有回归流程。

## 修复后重新执行与报告导出

已结束的报告提供“重新执行…”按钮。点击后只是打开执行表单，预选原来的环境及所有用例，方便重新跑完整链路；**点击“提交执行”才会入队**。使用最新保存的环境、用例与执行顺序，不沿用上一次提取的令牌。请检查用例是否被删除或改过顺序；回写到测试运行需要重新选择。

新报告与原报告互相提供链接，原始结果不会被覆盖。不会直接自动重试失败的 POST 或 DELETE 请求，也不会默认只选失败步骤而漏掉登录等前置步骤。

“导出 JSON 报告”只在任务结束后可用，包含每条用例的状态、耗时、断言、脱敏后的请求和响应摘要、跳过原因与回写情况。不会导出加密配置快照或环境认证凭据。CI 客户端同时输出 JSON 和 JUnit XML 报告。

## 统一用例库

测试用例页面以 `TestCase` 为场景主记录，显示系统维护的“手工”“自动化 · 接口”标记，可同时按产品、业务分类、执行方式和标题筛选。执行方式不是可随意填写的普通标签。已有 `is_automated` 标记仍兼容外部脚本用例；存在接口配置的用例会显示为接口自动化。

新建用例时填写业务分类、优先级、步骤和预期结果。选择“自动化 · 接口”后继续填写请求和断言。已有手工用例可以点击“配置接口自动化”，保留原来的用例编号、需求和历史。一个业务场景可以有多个接口执行配置；用例库只显示一条场景记录。

升级时会把尚未关联的旧接口配置纳入用例库，默认分类为“接口自动化”，保留原配置和报告，不按名称猜测合并。已有明确关联直接沿用。接口配置仍按账号隔离，共享用例的其他成员只能看到自己有权限的业务信息。用例详情可查看当前账号的自动化入口和升级后新增执行的最近结果；旧报告继续在执行记录中保留。

## 自动化套件与定时执行

1. 在“接口自动化”页面点击“新建套件”。
2. 选择环境、用例、失败停止策略、是否共享 Cookie，保存后可以“立即执行套件”。
3. 需要定时执行时勾选启用，设置首次时间和间隔。间隔支持 5～10080 分钟；60 为每小时，1440 为每 24 小时。不是 cron 表达式或按周工作日规则。
4. 详情页查看下次时间、触发错误及最近报告，随时可暂停定时执行。

`scheduler` 每 10 秒检查到期套件，只负责入队，实际请求仍由 Worker 执行。部署默认显示时区为 `Asia/Shanghai`，可通过 `.env` 的 `KIWI_TIME_ZONE` 修改；数据库保存 UTC。允许多实例调度，通过事务锁避免重复提交。同一套件前次排队或执行中时，本轮跳过；停机后只补一次到期检查，不连续重放错过的任务。

每次触发使用最新保存的用例和环境，再生成不可变执行快照。删除用例或修改产品导致配置失效时，记录提交错误，不静默减少执行数量。账号停用会暂停定时任务。套件不会自动绑定某个长期不变的测试运行；需要回写特定测试运行时使用单次执行表单。套件报告保留每条业务用例关联。

## Cookie 会话共享

单次执行或套件配置中勾选“共享 Cookie”，前序响应的 `Set-Cookie` 会用于后续匹配请求。遵循域名、Path、Secure 和过期/删除规则；不跟随重定向。显式配置的 Cookie 请求头优先，不与 CookieJar 自动头拼接。

CookieJar 仅存在于当前任务的内存中，每次运行重新建立，账号、套件和不同运行之间不共享。不写入环境或快照，报告隐藏 Cookie 头和已知 Cookie 值。这是服务端 HTTP 会话，不模拟浏览器 SameSite、JavaScript 或浏览器登录页行为；有 CSRF 令牌的业务仍需显式提取和传递相应令牌。失败响应设置的 Cookie 也遵循正常 HTTP 会话语义；要求登录成功才继续时请勾选失败停止。

本地演示可新增两条自动化用例：顺序 10 `POST /cookie/login`，JSON 为 `{"username":"demo","password":"demo-password"}`；顺序 20 `GET /cookie/me`，预期 200。选择共享 Cookie 后两条均应通过；关闭共享时第二条应返回 401。

## CI 触发与构建结果

套件详情点击“生成 CI 令牌”，明文只显示一次，服务器只保存哈希，有效期 90 天。令牌只授权当前套件，不使用浏览器登录 Cookie。轮换后旧令牌失效，也可随时撤销。CI 必须能够访问平台的 HTTPS 地址。

在 CI 的凭据管理中保存 `KIWI_CI_TOKEN`，设置 `KIWI_BASE_URL`（例如 `https://192.168.190.128:9443`）。使用自签证书时，将验证过的站点证书/签发 CA 配置为 `KIWI_CA_BUNDLE` 文件；证书需与访问地址匹配，不要关闭证书校验。然后在检出项目的工作目录执行：

```bash
python deployment/kiwi_ci.py --suite 1 --output api-report.json --timeout 900
```

脚本仅使用 Python 标准库。打印本次提交 UUID，可用 `--key 同一个UUID` 重试连接；在同一套件下该标识返回同一个任务，即使期间套件配置已经更新，也不重新执行。不同构建应使用不同 UUID，可设置 `KIWI_CI_KEY`。令牌只通过请求头发送，脚本不打印令牌、不跟随重定向、不自动重试提交。CI 本地等待超时不会取消已提交的任务，避免误认为目标服务未收到请求。

任务结束后生成 `api-report.json` 和 `api-report.xml`。退出码：0 为全部通过，1 为包含失败、异常、跳过或中断，2 为客户端配置、连接、认证或超时错误。CI 中直接使用该退出码决定构建是否通过，并归档报告。Jenkins 示例（凭据 ID 由实际配置替换）：

```groovy
pipeline {
    agent any
    environment { KIWI_BASE_URL = 'https://kiwi.example.test' }
    stages {
        stage('接口回归') {
            steps {
                withCredentials([string(credentialsId: 'kiwi-suite-token', variable: 'KIWI_CI_TOKEN')]) {
                    sh 'python3 deployment/kiwi_ci.py --suite 1 --output api-report.json'
                }
            }
        }
    }
    post {
        always {
            junit allowEmptyResults: true, testResults: 'api-report.xml'
            archiveArtifacts allowEmptyArchive: true, artifacts: 'api-report.json'
        }
    }
}
```

直接集成的接口：`POST /ai/api-testing/ci/suites/{id}/runs/`，请求头 `Authorization: Bearer <令牌>`、`Idempotency-Key: <UUID>`；无须请求体。轮询 `GET /ai/api-testing/ci/suites/{id}/runs/{run_id}/`。返回 `terminal` 表示结束，`passed` 表示整体通过。浏览器登录会话本身无法调用这两个 CI 接口。CI 只能读取本套件以 CI 方式提交的任务。

这里实现的是 CI 触发平台执行已有接口套件，暂不包含导入外部 pytest/Playwright 测试报告或托管任意脚本。

设计参考：[Testmo 自动化关联](https://support.testmo.com/hc/en-us/articles/37904613921549-Automation-Linking)。不同平台并非都强制统一存储；本项目选择统一场景入口、独立自动化执行配置和报告。Jenkins 示例参考其 [Pipeline 指南](https://www.jenkins.io/doc/book/pipeline/jenkinsfile/)和 [JUnit 步骤](https://www.jenkins.io/doc/pipeline/steps/junit/)。

## 回写已有测试执行

1. 编辑接口配置，在“关联平台测试用例”中选择同一产品的用例。这里只显示你有维护权限的用例，保存后标记为自动化。
2. 将这些平台用例加入一个未结束的测试运行。
3. 提交接口执行时选择该运行，并明确选择成功、失败对应的执行状态。
4. 报告中每条结果都会显示回写情况，测试运行中会更新执行状态、执行人和起止时间，历史记录含接口报告 ID。

回写要求账号具有修改测试执行的全局权限，并具有目标运行的修改权限。所选接口用例必须各自对应运行中唯一的一条执行；同一用例的多配置矩阵执行暂不支持自动映射。一个运行已有接口任务进行中时，会阻止再次提交，避免并发覆盖。提交后人工修改过的执行会跳过回写，并在报告中提示；执行时还会重新检查账号权限。

请求异常不会回写“断言失败”，以便区分基础设施故障和业务断言问题。报告中可能出现“接口通过，但回写失败”，应按回写提示人工核对。

## 停止与异常恢复

排队任务可以直接停止。执行中的任务停止后，当前请求结束并保存结果，剩余用例跳过。停止不会撤销目标服务已经收到的请求。

如果机器断电或 Worker 被强制终止，任务不会在重启后自动重试，避免重复创建订单等副作用。先停止所有连接该数据库的 Worker，再把指定任务标记为中断：

```bash
docker compose --env-file .env -f docker-compose.ai.yml stop worker
docker compose --env-file .env -f docker-compose.ai.yml exec web python manage.py api_recover_runs 任务UUID --workers-stopped
docker compose --env-file .env -f docker-compose.ai.yml up -d worker
```

保留已有结果；未保存结果的请求可能已经发送，需要人工核对服务后再决定是否重新创建任务。该恢复命令不会重新发送任何请求。

## 验证范围

`tcms.ai_assistant.test_api_automation` 使用临时数据库和本地真实 HTTP 服务验证请求、断言、超时、响应大小、重定向、权限隔离、幂等提交、配置快照、取消以及测试执行回写。

```bash
docker compose --env-file .env -f docker-compose.ai.yml --profile test run --build --rm --no-deps tests python manage.py test tcms.ai_assistant.test_api_automation --settings=tcms.settings.test --noinput
```

并发提交和多个 Worker 领取由 `APIQueueConcurrencyTests` 覆盖，在 SQLite 下跳过，在 MariaDB 下执行。使用部署指南中的独立 MariaDB 测试服务，可以连同这些测试一起验证：

```bash
docker compose --env-file .env -f docker-compose.ai.yml --profile test-mariadb run --build --rm tests-mariadb
```

界面的自动更新仅轮询结果，不重新触发测试请求。
