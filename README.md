# AI 热点资讯定时推送 Agent

这个 agent 会每天抓取公开 AI 信息源，使用 OpenAI-compatible API 整理成中文摘要，然后通过 SMTP 发到你的邮箱。

默认数据源：

- GitHub Trending：筛选 AI、LLM、Agent、RAG 等相关项目
- 官方博客/RSS：OpenAI、Anthropic、Google DeepMind、Google AI
- 技术博客：Simon Willison
- 热门讨论：Hacker News Algolia API

## 云端每天 08:00 推送

项目已加入 GitHub Actions 工作流：`.github/workflows/daily-ai-news.yml`。

它会在每天北京时间 08:00 运行，不需要你的电脑开机。GitHub Actions 的定时任务由云端执行，通常会在设定时间附近触发；如果你要求严格到分钟级的准点执行，建议部署到云服务器并使用系统 cron。

### 1. 上传到 GitHub

把这个项目推送到一个 GitHub 仓库。建议使用私有仓库，因为邮件配置和运行日志可能比较敏感。

### 2. 配置 Secrets

在 GitHub 仓库中打开：

`Settings -> Secrets and variables -> Actions -> New repository secret`

添加以下 Secrets：

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`，例如 `https://api.openai.com/v1` 或兼容服务地址
- `OPENAI_MODEL`，例如 `gpt-4.1-mini` 或兼容模型名
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USE_SSL`，例如 `false`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `EMAIL_FROM`
- `EMAIL_TO`

本地 `.env` 不要上传到 GitHub；项目已通过 `.gitignore` 忽略它。

### 3. 手动测试云端运行

进入 GitHub 仓库：

`Actions -> Daily AI News Email -> Run workflow`

确认邮件能收到后，后续会每天北京时间 08:00 自动推送。

## 本地测试

只生成摘要，不发邮件：

```powershell
python agent.py --dry-run
```

生成摘要并发送邮件：

```powershell
python agent.py
```

## Windows 本地定时任务备用方案

如果仍想在本机运行，可以安装 Windows 任务计划程序任务：

```powershell
.\install_windows_task.ps1
```

任务名称是 `AIHotNewsEmailAgent`，会按当前 Windows 本地时间每天 08:00 执行。这个方案仍然要求电脑在执行时间开机并联网。

## 调整信息源

编辑 `config.json`：

- `rss_feeds`：增加或删除官方博客/RSS
- `hacker_news.queries`：调整 HN 搜索关键词
- `github_trending.languages`：调整 GitHub Trending 语言
- `github_trending.keywords`：调整 AI 项目筛选关键词

## 说明

Anthropic 官网没有稳定公开的官方 RSS，因此配置里使用了 `Olshansk/rss-feeds` 从 Anthropic 官方页面生成的公开 RSS，链接仍指向 Anthropic 官方文章。
