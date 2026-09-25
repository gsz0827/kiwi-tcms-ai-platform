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

首页和业务页面左侧统一提供一个“项目配置”入口，进入独立导航页后可选择“新建产品”和“维护 AI 规则包”。产品分类统一在新建产品表单中选择；如果没有合适的分类，使用表单里的“新建分类”入口。创建分类和产品分别要求 `management.add_classification`、`management.add_product` 权限；产品创建后 Kiwi 会自动生成默认用例分类、`unspecified` 版本和构建。

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
| 分析按钮和生成按钮 | 分别提交分析任务和生成任务 |
| 任务详情页 | 在内容安全策略启用时仍能轮询状态 |

上面两条 `docker compose ... run` 命令都带 `--build`，不要省略：`tests` 与 `tests-mariadb` 在 Compose 里固定引用 `kiwi-tcms-ai:test` 镜像，而 `run` 默认不会重建镜像，省略后会跑在旧镜像的代码上。等价且更省心的写法是 `make ai-test`（内部固定先 build 再 run）。

`make ai-test` 只跑 `tcms.ai_assistant`，因为 Compose 里 `tests-mariadb` 的命令写死了这个应用名。平台改动过 `tcms/core`、`tcms/testcases`、`tcms/urls.py` 等上游代码，只跑应用测试覆盖不到，因此发版前还要跑一次全量套件：

```bash
make ai-test-full
```

上游测试依赖 `parameterized` 与 `tcms-api`，由 `requirements/ai-test.txt` 装进测试镜像，缺失时相关模块会以 `ModuleNotFoundError` 整体报错而不是真正运行。

补齐依赖后，全量套件当前会报一批 `Duplicate entry 'AnonymousUser'`（SQLite 下表现为 `UNIQUE constraint failed: auth_user.username`）。已确认这不是平台代码的问题，而是上游 RPC 用例的隔离问题：

| 验证方式 | 结果 |
| --- | --- |
| `python manage.py test tcms.rpc.tests` 单独运行 | 358 个用例全部通过 |
| `tcms.rpc.tests.test_version` 单独运行 | 通过 |
| `kiwi_auth.tests.test_backends` + `tcms.rpc.tests.test_version` | 通过 |
| 全量套件同进程运行 | RPC 用例在 fixture 反序列化阶段报匿名用户重复 |

`tcms/rpc/tests/utils.py` 里的 `serialized_rollback = True`、`tcms/settings/common.py` 里的 `ANONYMOUS_USER_NAME` 以及 django-guardian 3.3.3 都是上游原样内容，本平台未改动。触发条件是 guardian 的匿名用户被写入数据库后，`serialized_rollback` 的快照又插入一次；具体是哪个用例把它写进了数据库尚未定位。因此 RPC 覆盖请改用单独命令：

```bash
make ai-test-rpc
```

## 导航：侧边栏与上游菜单的关系

本平台用中文侧边栏替代上游顶部的横向菜单，但 `SETTINGS.MENU_ITEMS` 中侧边栏没有提供入口的条目不能就此消失——插件通过 `kiwitcms.plugins` 注册的 MORE 菜单正是追加在该列表最后一项上的。侧边栏的「更多功能」分组由 `platform_extra_menu` 模板标签生成：取出 MENU_ITEMS 中侧边栏尚未覆盖的条目，剔除与侧边栏重复的链接，插件部分则原样保留。

维护时注意三点：

- 侧边栏只在已登录时渲染，检查插件菜单是否出现必须以登录态访问，匿名请求只会拿到登录页；
- `tcms/settings/common.py` 约定 MENU_ITEMS 的最后一项固定是留给插件扩展的 MORE，判断插件分组依赖这个位置；
- 侧边栏新增入口时，若该入口对应上游 MENU_ITEMS 里的链接，要把它的 URL 名补进 `ai_navigation.SIDEBAR_MENU_URLS`，否则同一个页面会在「更多功能」里再出现一次。

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

当前 Worker 不使用分布式租约，也不会自动判断另一个进程是否已经死亡。它启动时不会批量把 `running` 任务改成失败。收到正常停止信号时，会完成当前任务后退出，不再领取新任务；Compose 最多等待 11 分钟后强制停止。

正常完成的任务不需要恢复。机器异常断电、进程被强制停止等情况下：

1. 停止连接同一数据库的**所有** Worker（包括其他机器上的实例）。本机执行 `docker compose -f docker-compose.ai.yml stop worker`。
2. 在任务中心确认中断任务 ID，并检查它是否已保存草稿、报告等业务结果。
3. 先预览，再按指定任务 ID 恢复。将下方 `任务UUID` 替换为实际 ID：

```bash
docker compose -f docker-compose.ai.yml run --rm --no-deps worker \
  python manage.py ai_recover_jobs 任务UUID

docker compose -f docker-compose.ai.yml run --rm --no-deps worker \
  python manage.py ai_recover_jobs 任务UUID --apply --workers-stopped

docker compose -f docker-compose.ai.yml start worker
```

恢复只把选中的执行中任务标为失败、取消中的任务标为已取消；排队和已结束任务保持原样。`--workers-stopped` 是管理员确认，不是自动检测。命令不会回滚已经保存的业务结果，也不会自动重试。确认结果后，再决定是否在页面中重试，避免重复产生报告或补充用例。

## 升级

先备份数据库、附件和 `.env`。停止 Web 和 Worker 后构建并执行迁移，避免旧代码与新数据结构同时运行：

```bash
docker compose -f docker-compose.ai.yml stop web worker
docker compose -f docker-compose.ai.yml build
docker compose -f docker-compose.ai.yml run --rm migrate
docker compose -f docker-compose.ai.yml up -d
```

确认迁移成功后再执行最后一步。运行中的任务若被停止，按上面的恢复步骤处理。不要随意更换 SECRET_KEY，否则已保存的模型密钥无法解密。
