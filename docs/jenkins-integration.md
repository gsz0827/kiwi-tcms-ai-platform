# Jenkins 集成

## 已实现的链路

Jenkins → 套件专用 Bearer 令牌 → 平台队列与不可变快照 → 原有 Web/API 执行器 → JSON/JUnit → Jenkins 构建结果。

`Jenkinsfile` 从 main 检出平台客户端；可选择接口或 Web 套件。环境参数和认证仍由平台管理，不写进 Jenkinsfile。通过为 SUCCESS，测试失败/未完整执行为 UNSTABLE，连接、认证或配置错误为 FAILURE。JSON 与 JUnit XML 作为构建附件保存。平台自己的结果页面、失败截图和 Allure 报告仍保留，访问需平台账号权限；CI 令牌不授权读取任意附件、审批报告或关闭缺陷。

## 本机入口

Jenkins：`http://localhost:9081`，任务名 `Kiwi-Automation`。只绑定本机地址，不挂载 Docker socket、不使用特权模式。管理员与 CI 令牌存放在仓库以外的受保护目录；密码不出现在流水线控制台。

演示项目包含接口和 Web 各一套通过/失败套件，用于验证 Jenkins 判定与报告，不调用真实业务接口或 AI 模型。示例默认使用调试执行，不进入正式测试报告。

## 自己的套件

1. API：测试套件详情 → Jenkins / CI 接入。Web：编辑测试套件 → Jenkins / CI 接入。
2. 生成专用令牌，明文只显示一次；90 天到期，可轮换、撤销。旧令牌轮换后失效。
3. 在 Jenkins 凭据中新增 **Secret text**；选择套件 ID、类型与该凭据 ID，点击参数化构建。
4. Web/API 正式执行选择 formal，并填写计划、构建（可选环境）。计划版本须匹配构建版本，业务用例须纳入计划，账号须有正式执行权限。不会自动审批或回写正式报告。
5. Jenkins 中查看 Test Result 和构建附件；JSON 的 report_url 对应平台执行详情。

同一个构建重试使用相同 UUID，不重复创建执行。新构建创建新记录。客户端超时不会自动取消已提交的平台任务；请在平台查看或取消，不能盲目反复点构建。

## 独立部署

先启动平台并完成初始化，再为套件生成 CI 令牌。需要 Docker Compose。可将令牌通过受保护环境变量 `KIWI_API_CI_TOKEN`/`KIWI_WEB_CI_TOKEN` 传给初始化脚本，同时设置对应 `KIWI_API_SUITE_ID`/`KIWI_WEB_SUITE_ID`；也可以启动后手动加入 Jenkins 凭据。

```bash
python3 deployment/jenkins/bootstrap.py --directory /absolute/private/jenkins-secrets --ca /absolute/public/kiwi-ca.pem
export KIWI_JENKINS_SECRET_DIR=/absolute/private/jenkins-secrets
export KIWI_DOCKER_NETWORK=your-platform-network
docker compose -f docker-compose.jenkins.yml up -d --build jenkins
```

私有目录必须在仓库外，权限应限制到部署账号和 Jenkins。初始化脚本不覆盖已有文件；初始管理员信息见该目录的 `admin-login.txt`。Jenkins 数据在独立卷中，停止容器不丢失构建或配置。demo 服务仅用于本机验收，需要时单独启动，不能作为业务测试服务。

远程 Jenkins 设置可访问的平台 HTTPS 地址并清空 CONNECT_HOST，将已验证的 CA 文件通过节点环境的 KIWI_CA_BUNDLE 提供。不能关闭 HTTPS 证书校验。本机容器使用 `https://localhost:8443` 与 CONNECT_HOST=kiwi-web：TCP 连接内部容器，SNI 和证书验证仍为 localhost。它不是跳过证书校验。

API 演示地址需白名单 `KIWI_API_ALLOWED_ORIGINS=http://kiwi-ci-demo:8080`；白名单只开放明确来源，不能使用通配。Web 演示访问平台登录页，沿用本机允许自签证书的测试环境设置；Jenkins → 平台的控制连接始终验证证书。

本机使用一个受信任执行槽、1 GB 容器内存上限和 512 MB Java 堆。公开部署应使用 HTTPS、细粒度角色及独立构建节点，不开放匿名权限。Jenkins LTS 镜像和插件需持续维护，本次验收版本以容器实际输出为准。

参考：[Jenkins Docker 安装](https://www.jenkins.io/doc/book/installing/docker/)、[Jenkinsfile](https://www.jenkins.io/doc/book/pipeline/jenkinsfile/)、[JUnit](https://www.jenkins.io/doc/pipeline/steps/junit/)、[凭据](https://www.jenkins.io/doc/book/using/using-credentials/)。

## 发布前门禁与模拟发布

Jenkins 现支持可选的“正式执行 → 人工确认归档 → 报告审批 → 发布准入检查 → 模拟发布”链路。
**模拟阶段没有任何真实部署命令，也不会自动审批、归档、关闭缺陷或修改报告快照。**

### 使用方法

1. 更新 Jenkins 使用的仓库代码。已有任务可能需要先运行一次普通构建，让 Jenkinsfile 刷新参数。
2. 选择 TEST_TYPE=api 或 web，填写套件与套件令牌凭据。
3. WEB_MODE 选择 formal。此参数名为兼容旧任务保留，现在对 Web/API 都生效。
4. 填写 PLAN_ID、BUILD_ID，按需填写 ENVIRONMENT_ID；开启 RELEASE_CHECK（默认关闭）。
5. 套件执行完成后，在平台找到本次执行，人工确认归档，并由有权限的账号审批归档报告。
6. Jenkins 最多等待 10 分钟。通过后进入模拟阶段，并在边界再次查询平台；查看 kiwi-release-gate.json 与 kiwi-release-simulation.json。

整条流水线仍有 20 分钟总超时，测试执行耗时可能缩短人工审批可用时间；超时不放行。
正常调试构建不需开启 RELEASE_CHECK。调试结果、历史旧执行或未归档报告不能用于发布。

### 判定与证据绑定

- CI 令牌仅能检查自己套件、自己账号、trigger=ci 的执行，不能选择任意报告。
- 同时核对本次 Jenkins 构建的固定提交 UUID、自动化执行 UUID、项目、测试计划、构建及版本。
- Web/API 正式提交复用页面的计划、构建、环境和业务用例校验；旧的空请求体 API CI 调用仍兼容，但不能用于发布门禁。
- 报告必须来自该执行的归档记录，处于当前版本、已批准且保留有效签字。
- 校验报告快照哈希，并将冻结快照与当前执行数据比较。审批后改结果、增加/删除执行项或改变上下文，都阻断，不篡改历史快照。
- 重新按当前项目规则检查未关闭缺陷及执行指标。特批必须仍有有效授权、理由，并匹配当时评估的规则和风险；新增风险不能继承旧特批。
- 缺少归档或待审批时等待；驳回、构建不匹配、证据损坏、令牌失效、网络/证书异常、等待超时均不放行。
- 不返回请求头、环境密钥或截图；门禁判定及上下文写入既有操作审计。

Jenkins 保持更严格的流水线策略：自动化失败或未完整执行先标记 UNSTABLE，并通过
skipStagesAfterUnstable 跳过后续阶段；平台的风险放行不会把失败构建自动改成成功。

### 文件与接口

- Jenkinsfile：RELEASE_CHECK 参数、发布准入检查、模拟发布和构建附件。
- deployment/kiwi_release_gate.py：独立、失败默认拒绝的检查客户端；只检查已有执行，不提交新测试。
- POST /web-testing/ci/suites/{suite_id}/runs/{run_uuid}/release-gate/
- POST /ai/api-testing/ci/suites/{suite_id}/runs/{run_uuid}/release-gate/

请求使用套件 Bearer 令牌，JSON 仅允许 plan_id、build_id、submission_key。
allowed=true 且 status=allowed 才是通过；waiting 不是通过。报告、构建或提交标识不能靠客户端替换绕过。
模拟记录明确包含 simulation=true、deployment_performed=false。

### 边界

这是单套件、单次正式执行对应归档报告的发布准入检查，不会自动证明整个迭代的所有套件都已覆盖。
BUILD_ID 是平台构建记录；真实发布时还必须把它与实际 Git 提交及不可变制品摘要建立可信绑定。
RELEASE_CHECK 可选是为了兼容普通测试任务；真正生产发布任务应由受保护配置强制开启，
并在受控部署入口再次校验，不能把可编辑的 JSON 文件当成永久发布许可证。
真实部署、Webhook 自动触发、跨套件汇总发布评估不在本次改动范围。
