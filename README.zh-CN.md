# DailyInfo

中文 | [English](README.md)

DailyInfo 是基于 **Python + FreshRSS + DeepSeek** 的 AI 科研情报与每日推荐工具，支持多信息源聚合采集（期刊 RSS / arXiv / AI 新闻 / GitHub Trending / HuggingFace 等）、自定义主题与关键词筛选、AI 中文摘要与相关性评分、推荐理由生成、每日精选简报，以及 **邮箱 / 飞书 / Discord** 多渠道推送。

覆盖 Docker Compose 服务编排、RSS / 网页 / API 数据采集、arXiv 主题检索、SQLite 分析缓存与投递状态管理、SMTP 邮件通知、飞书 Webhook 签名校验、跨日去重与失败恢复，并提供统一 CLI，支持外部定时调度。

通过 `config/topics.json` 设置研究兴趣，通过 `config/delivery.json` 设置推送渠道。
默认邮箱接收论文精选，飞书接收综合简报，Discord 可按需启用。查看[示例精选](examples/daily_digest.preview.md)和[配置指南](docs/recommendations.md)。

## 工作原理

一条流水线,五个相互独立的信息来源:

```text
FreshRSS / 网页抓取 / API 数据源
  -> dailyinfo run
  -> AI 摘要 + 结构化简报
  -> dailyinfo recommend [--search-arxiv]
  -> 主题相关性评分 + 推荐理由 + 每日精选
  -> dailyinfo deliver
  -> 邮箱 / 飞书 / Discord + 独立投递状态
```

1. **采集** — 五条独立流水线从 40+ 个 feed、页面和 API 拉取信息。一条流水线失败不影响其他。
2. **归纳** — DeepSeek 为每个数据源生成一份可读的中文简报,面向研究者优化:这些论文讲了什么、为什么重要、哪些值得细看。
3. **推荐与推送** — 根据研究主题筛选内容、生成推荐理由，并按分类推送到邮箱、飞书或 Discord；成功投递后去重，失败渠道可以单独恢复。

每天收到少量与研究方向相关的论文、文章和技术动态，每条附中文摘要、推荐理由与原文链接。

## 特性

| | |
|---|---|
| **五条流水线** | 期刊论文(30+ 期刊,含中文水利期刊)· AI 资讯 · arXiv CS.AI(每日最多 500 篇)· GitHub Trending + HuggingFace 模型 · 院所动态 |
| **中文优先简报** | AI 生成中文摘要;主模型 API 故障时自动降级到智谱 GLM 备用模型 |
| **配置驱动** | 在 `config/sources.json` 中添加 RSS、网页或 API 数据源,无需改代码 |
| **主题推荐** | 设置主题描述、关键词、排除词、相关性阈值与每日分类数量上限 |
| **多渠道推送** | 邮箱论文精选、飞书综合简报、可选 Discord；各目标独立记录状态，支持跨日去重 |
| **幂等,可安全重跑** | 已有今日简报的数据源自动跳过;已推送文件不会重复发送 |
| **高容错** | 指数退避重试、AI 响应不完整时自动拆分批次、数据源级隔离 |
| **调度无关** | 自带 cron、systemd timer 或 agent runtime——DailyInfo 负责流水线,不负责时钟 |

## 效果展示

把截图放到 `pictures/` 并使用下面的文件名后,会自动显示在这里。

### Discord 简报推送

#### 学校资讯

![Discord 学校资讯简报](pictures/discord-university-updates.png)

#### 期刊论文

![Discord 期刊论文简报](pictures/discord-journal-papers.png)

#### arXiv 论文

![Discord arXiv 论文简报](pictures/discord-arxiv-papers.png)

#### AI 资讯

![Discord AI 资讯简报](pictures/discord-ai-news.png)

#### Code Trending

![Discord Code Trending 简报](pictures/discord-code-trending.png)

## 数据目录

默认数据根目录是 `~/.myagentdata/dailyinfo/`,可通过 `DAILYINFO_DATA_ROOT` 覆盖。

```text
~/.myagentdata/dailyinfo/
├── freshrss/data/       # FreshRSS SQLite + 配置
├── briefings/           # 待推送 Markdown
│   ├── papers/
│   ├── ai_news/
│   ├── arxiv/
│   ├── code/
│   └── resource/
└── pushed/              # 已推送归档
    ├── papers/
    ├── ai_news/
    ├── arxiv/
    ├── code/
    └── resource/
```

## 快速开始

```bash
git clone https://github.com/zhuanglaihong/dailyinfo.git
cd dailyinfo

cp .env.example .env
# 填写 DEEPSEEK_API_KEY，以及所选渠道的 SMTP / 飞书配置。
# Discord 为可选渠道，使用时再填写 DISCORD_BOT_TOKEN。

uv sync --python python3
uv pip install -e .
dailyinfo install

dailyinfo start
dailyinfo run
dailyinfo recommend
dailyinfo deliver --dry-run
dailyinfo deliver
```

`dailyinfo install` 会校验 `.env`、创建本地数据目录并安装依赖。它不会写入 crontab;定时调度由你的 cron 或 agent runtime 负责。

## 常用命令

### 每日主题精选（邮箱 / 飞书 / Discord）

编辑 `config/topics.json` 设置研究主题，编辑 `config/delivery.json` 设置渠道与分类，
在 `.env` 中填写 SMTP 和飞书 Webhook。默认邮箱收论文、飞书收综合精选。

```bash
dailyinfo run
dailyinfo recommend --no-ai       # 可选：先生成关键词预览
dailyinfo recommend --search-arxiv # AI 推荐；可同时按主题检索 arXiv
dailyinfo deliver --dry-run        # 预览推送内容
dailyinfo deliver                  # 推送到所有已启用渠道
```

支持分类限额、中文推荐理由、AI 缓存和按渠道/目标/内容独立去重。
原有 Discord 推送和 Web 发布继续可用。配置、失败恢复和调度见
[每日推荐指南](docs/recommendations.md)。

| 命令 | 用途 |
|------|------|
| `dailyinfo install` | 校验环境并创建数据目录 |
| `dailyinfo start` / `stop` / `restart` | 管理 FreshRSS 容器 |
| `dailyinfo run` | 运行全部简报流水线 |
| `dailyinfo run -p 1` | 流水线 1：期刊论文 |
| `dailyinfo run -p 2` | 流水线 2：AI 资讯 |
| `dailyinfo run -p 3` | 流水线 3：arXiv CS.AI |
| `dailyinfo run -p 4` | 流水线 4：代码趋势 |
| `dailyinfo run -p 5` | 流水线 5：院所资讯 |
| `dailyinfo run -f all` | 强制重生全部数据源 |
| `dailyinfo push` | 推送待处理简报到 Discord 并归档 |
| `dailyinfo push -d 2026-04-22` | 推送指定日期简报 |
| `dailyinfo push -c weekly` | 只推送每周回顾 |
| `dailyinfo weekly` | 从近期简报生成每周 AI 资讯回顾 |
| `dailyinfo status` | 查看当天简报和归档数量 |
| `dailyinfo logs` | 查看执行日志 |
| `dailyinfo cache-clear` | 清除指定源的 FreshRSS 缓存(默认 `arxiv_cs_ai`) |
| `dailyinfo clean-cache` | 删除超过 N 小时的 FreshRSS 缓存文件(默认 24h) |

## 环境变量

| 变量 | 用途 |
|------|------|
| `DEEPSEEK_API_KEY` | `dailyinfo run` 使用的 DeepSeek API key(主模型) |
| `DISCORD_BOT_TOKEN` | `dailyinfo push` 使用的 Discord bot token |
| `DISCORD_CHANNEL_PAPERS` / `_AI_NEWS` / `_ARXIV` / `_CODE` / `_RESOURCE` | 可选分类频道 ID |
| `FRESHRSS_USER` | FreshRSS 用户名 |
| `FRESHRSS_PASSWORD` | FreshRSS 初始密码 |
| `DAILYINFO_DATA_ROOT` | 覆盖默认数据根目录 |
| `GLM_API_KEY` | 智谱 GLM API key(可选,用于备用模型) |
| `DAILYINFO_ENV` | 环境：`prod` / `dev` / `staging`(默认 `prod`) |
| `DAILYINFO_FALLBACK_MODEL` | 主模型空响应时的备用模型(默认 `glm-5.3-flash`,智谱模型名) |

## 调度和 Agent 分工

DailyInfo 不负责调度。推荐分工如下：

| 职责 | 归属 |
|------|------|
| FreshRSS 容器和本地数据目录 | DailyInfo |
| `dailyinfo run` 生成 Markdown | DailyInfo |
| `dailyinfo push` 推送和归档 | DailyInfo |
| 定时执行 | cron、myopenclaw、openclaw 或 agent runtime |

## 文档

- [系统架构](architecture.md)
- [CLI 参考](cli.md)
- [Agent 配置](agent-config.md)
- [数据源说明](sources.md)

## License

本项目基于 [iHeadWater/dailyinfo](https://github.com/iHeadWater/dailyinfo) 扩展，增加个人主题推荐与多渠道精选推送，保留原项目版权与许可证声明。

BSD 3-Clause License. See [LICENSE](LICENSE) for details.
