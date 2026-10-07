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
4. Web 正式执行选择 formal，并填写计划、构建（可选环境）。计划版本须匹配构建版本，业务用例须纳入计划，账号须有正式执行权限。不会自动审批或回写正式报告。
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
