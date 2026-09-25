# Kiwi TCMS AI 智能测试管理平台

基于 [Kiwi TCMS](https://github.com/kiwitcms/Kiwi) 二次开发的个人实践项目，将 AI 辅助需求分析、测试用例设计与原有测试管理流程结合，用于学习、演示和软件测试项目实践。

本仓库保留 Kiwi TCMS 的原始代码、提交历史和许可证。原有测试管理能力来自上游项目；本项目的主要扩展位于 `tcms/ai_assistant/` 和平台导航、资源目录相关模板中。

## 主要扩展

- **AI 测试助手**：需求分析、测试用例生成与编辑、用例评审、覆盖分析和补充用例。
- **用例库与接口自动化**：统一展示手工和自动化用例，按业务分类筛选；自动化套件支持手动、定时和 CI 触发，支持响应变量提取、单次运行 Cookie 共享、断言、失败停止、脱敏 JSON/JUnit 报告与测试运行回写。见 [接口自动化指南](docs/api-automation.md)。
- **个人模型配置**：各账号维护自己的模型配置；未配置时提示配置；API 密钥加密保存，调用记录按账号隔离。
- **后台任务**：通过独立 worker 执行 AI 任务，显示任务阶段和进度，支持取消及重试相关操作。
- **缺陷与回归**：失败执行分析、缺陷草稿及已有缺陷链接关联、缺陷生命周期管理、回归验证记录。
- **报告与追踪**：执行报告、迭代报告、报告版本及审批、发布门禁、需求与用例等资源的追踪关系。
- **使用体验**：中文界面优化、统一侧边导航、资源树及详情查看、同一项目成员共享的自定义目录。

具体功能及权限以当前代码和实际页面为准；AI 输出需要测试人员评审后使用。

## 技术组成

Python、Django、MariaDB、Nginx、uWSGI、Docker Compose、Django 模板与 JavaScript，以及兼容 OpenAI 接口格式的模型服务。

## 项目结构

```text
tcms/ai_assistant/                AI 功能、后台任务、模型、迁移与测试
tcms/templates/include/          平台导航和资源树组件
tcms/testcases/                  测试用例
tcms/testplans/                  测试计划
tcms/testruns/                   测试执行
tcms/locale/                    界面翻译
etc/nginx.conf                  Web 服务配置
README.rst                      Kiwi TCMS 上游说明
```

## 运行说明

本项目提供独立的 `docker-compose.ai.yml`，从当前仓库构建镜像，并启动 MariaDB、数据库迁移、Web 和 AI worker。根目录原有的 `docker-compose.yml` 仍是上游示例，不包含这些部署步骤。

需要 Docker Engine 与 Docker Compose v2。首次构建需要下载 Python、Node 和系统依赖。

```bash
cp .env.example .env
# 分别执行三次，将生成的不同值填入 .env 的三个密钥/密码字段
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'

docker compose -f docker-compose.ai.yml up --build -d
# 等待 migrate 成功、web 启动后完成站点和管理员初始化
docker compose -f docker-compose.ai.yml exec -e KIWI_TENANTS_DOMAIN=localhost web \
  python manage.py initial_setup
```

访问 `https://localhost:9443/`，AI 入口为 `/ai/`。本地自签名证书会触发浏览器证书提示。默认端口仅绑定本机，注册默认关闭。数据库、附件和证书使用独立的持久化卷。此配置用于本地学习与演示；对外部署需要配置域名、可信证书和备份。

Web 和 worker 使用同一个 `KIWI_SECRET_KEY`。它用于加密个人模型密钥，已有数据时不要随意更换。登录后，在模型管理页面配置模型服务地址、模型名称与自己的 API 密钥。

详细的启动、验证、升级和异常任务恢复步骤见 [部署与验证指南](docs/ai-platform-operations.md)。

## 核心可靠性行为

- 新需求表单携带提交标识，同一账号重复提交同一份表单会返回原任务，包括任务已结束的情况；新打开的表单允许主动提交相同内容。
- 需求、初始版本和后台任务在同一事务中保存，入队失败时整体回滚。
- Worker 启动不会修改其他 Worker 正在处理的任务。异常退出留下的任务需在停止所有 Worker 后按 ID 人工恢复，不会自动重放可能已经产生结果的操作。
- AI 生成结果先进入草稿，经人工复核后导入。任务进度是业务阶段进度，不代表模型生成的精确百分比。

本机运行配置、数据库备份、上传附件、HTTPS 私钥和真实 `.env` 不包含在仓库中。

## 上游与许可

上游项目：[kiwitcms/Kiwi](https://github.com/kiwitcms/Kiwi)。原始说明见 [README.rst](README.rst)，许可证见 [LICENSE](LICENSE)。本项目为个人二次开发实践，不是 Kiwi TCMS 官方版本。
