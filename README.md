# Kiwi TCMS AI 智能测试管理平台

基于 [Kiwi TCMS](https://github.com/kiwitcms/Kiwi) 二次开发的个人实践项目，将 AI 辅助需求分析、测试用例设计与原有测试管理流程结合，用于学习、演示和软件测试项目实践。

本仓库保留 Kiwi TCMS 的原始代码、提交历史和许可证。原有测试管理能力来自上游项目；本项目的主要扩展位于 `tcms/ai_assistant/` 和平台导航、资源目录相关模板中。

## 主要扩展

- **AI 测试助手**：需求分析、测试用例生成与编辑、用例评审、覆盖分析和补充用例。
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

本项目当前开发环境为 Windows + WSL Ubuntu + Docker Compose。开发环境首页为 `https://localhost:9443/`，AI 入口为 `/ai/`；端口与 HTTPS 证书需根据自己的环境配置。

仓库根目录的 `docker-compose.yml` 是上游示例，默认使用上游镜像；仅启动该文件不会自动包含本项目二次开发代码。运行本项目需要构建本仓库镜像，或将本仓库的 `tcms` 目录挂载到兼容的 Kiwi 运行容器中，并配置数据库、持久化目录和独立的 AI worker。

容器内常用命令：

```bash
# 初始化或升级数据库
/venv/bin/python /Kiwi/manage.py migrate --noinput

# 创建管理员
/venv/bin/python /Kiwi/manage.py createsuperuser

# 独立服务中持续运行 AI worker
/venv/bin/python /Kiwi/manage.py ai_worker

# 使用隔离测试数据库运行 AI 模块测试
/venv/bin/python /Kiwi/manage.py test tcms.ai_assistant
```

Web 与 worker 需要连接同一数据库并使用一致的 Django SECRET_KEY。每个账号登录后，在模型管理页面配置模型服务地址、模型名称与自己的 API 密钥。

本机运行配置、数据库备份、上传附件、HTTPS 私钥和真实 `.env` 不包含在仓库中。部署时请自行配置密码与密钥，不要使用上游示例中的默认值。

## 上游与许可

上游项目：[kiwitcms/Kiwi](https://github.com/kiwitcms/Kiwi)。原始说明见 [README.rst](README.rst)，许可证见 [LICENSE](LICENSE)。本项目为个人二次开发实践，不是 Kiwi TCMS 官方版本。
