# 每日主题推荐与多渠道推送

DailyInfo 保留原有 `run`、Discord `push` 和 Web 发布，并增加一条独立流程：

```text
dailyinfo run → canonical publications
                         ↓
         dailyinfo recommend [--search-arxiv]
                         ↓
             recommendations/YYYY-MM-DD/daily.json + daily.md
                         ↓
         dailyinfo deliver → 邮箱 / 飞书 / Discord
```

## 1. 配置主题

编辑 `config/topics.json`。默认的 AI for Science 是可编辑示例，不代表已了解你的兴趣。

每个主题包含 `description`、`include_terms`、`exclude_terms`、`categories`、
`lookback_hours`、`min_relevance`、`max_candidates` 和 `daily_limits`。
AI 会结合主题描述、标题、已有摘要和标签评分；包含词用于优先排列候选，
不是强制条件，因此可以推荐未直接出现关键词但实质相关的内容。排除词是硬性规则。

支持现有分类：`papers`、`arxiv`、`ai_news`、`code`、`resource`。
博客和深度文章可作为 `ai_news` 的 RSS 来源配置在 `config/sources.json`。
首版暂不新增 `article` 分类，以兼容 Publication v1。

论文 RSS 有摘要时，会把最多 6000 字的去 HTML 内容提供给摘要模型；
没有摘要时仍只能依据标题。原始摘要通过 RSS 获取，不自动下载或阅读全文。

```bash
dailyinfo run
dailyinfo recommend --no-ai  # 无 AI 调用，写 preview.json/preview.md
dailyinfo recommend          # AI 评分，写 daily.json/daily.md
```

推荐读取指定日期已经生成的结构化 publications，即使 Markdown 已经被 Discord 归档也可读取。
`lookback_hours` 是候选的原始发布时间窗口；发布时间未知时用采集时间并显式标注。
它不会自动读取所有历史简报。无候选时不覆盖原有日报；有候选但无相关结果时生成空的主题日报。

`--search-arxiv` 根据主题的 `arxiv_queries` 同时主动查询 arXiv，获取原始摘要和作者，
不依赖 FreshRSS。查询使用 arXiv 语法，可填写中英文术语的多条查询；
同次最多 10 条独立查询，每条最多 50 篇，请求间隔 3 秒。

```bash
dailyinfo recommend --search-arxiv
dailyinfo recommend --date 2026-10-07
```

AI 分数是推荐信号，不是科学结论或新闻真假证明。每条内容显示原文链接、来源、
日期、摘要和中文推荐理由。首版按标识或 URL 去重；同一事件的不同媒体报道尚未语义聚合。
DOI/arXiv 标识能跨源去重，普通 URL 保持原有来源身份。

AI 分析存储在 `state/recommendations.sqlite3`：主题、模型、提示词版本和内容相同时复用。
AI 响应格式错误或调用失败会返回非零退出码，不覆盖已完成日报；成功批次缓存保留供下次恢复。
`--force` 重新评分。无 AI 的规则预览与 AI 日报分开，不能通过 `deliver` 误发。

## 2. 配置渠道

编辑 `.env`，不要提交实际授权码和 Webhook。

```dotenv
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=researcher@example.com
SMTP_PASSWORD=邮箱服务商提供的SMTP授权码
DAILYINFO_EMAIL_TO=reader@example.com
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/替换为实际地址
FEISHU_WEBHOOK_SECRET=可选的机器人签名密钥
```

465 端口的邮箱通常使用 `SMTP_SECURITY=ssl`。必须使用加密连接。
飞书请在目标群添加自定义机器人，获取 Webhook；配置了签名校验时同时填写签名密钥。
飞书采用富文本消息，单次载荷上限为 19 KB；超限会在发送前返回错误，
通过减少 `daily_limits` 或拆分路由分类调整，不自动截断论文摘要。

`config/delivery.json` 默认启用：邮箱发送 `papers,arxiv`，飞书发送
`papers,arxiv,ai_news,code`。Discord 精选路由默认关闭；启用后填写
`DISCORD_DIGEST_CHANNEL` 和原有 `DISCORD_BOT_TOKEN`。
多份路由可使用不同 `target_env`，例如 `TEAM_EMAIL_TO`；配置名即可，不写真实地址。

在 `DAILYINFO_ENV=dev/staging` 时，目标和飞书签名密钥须使用 `_DEV/_STAGING`
后缀，不会回退到生产目标。SMTP 登录信息和 Discord Bot Token仍共用，
如果需要分离账号，应通过独立 `DAILYINFO_ENV_FILE` 配置整套环境。

```bash
dailyinfo deliver --dry-run      # 检查分类路由；不发消息、不写投递状态
dailyinfo deliver --sink email
dailyinfo deliver --sink feishu
dailyinfo deliver               # 尝试所有已启用的路由；互不阻塞
```

也可以直接发送已有完整分类简报，不经过推荐：

```bash
dailyinfo publish --sink email --categories papers,arxiv
dailyinfo publish --sink feishu --categories ai_news,code
```

`publish --sink all` 保留原有 Discord + Web 语义。推荐渠道用 `deliver`。
`dailyinfo push` 仍使用原有 Discord 分类频道。
`install` 现在只要求 AI Key，不强制要求 Discord Token；启用哪个渠道，配置哪个渠道。

## 3. 重跑、恢复与调度

日报渠道状态保存在 `state/notifications.sqlite3`。投递键包含内容、渠道和目标地址的哈希，
不保存明文地址、Webhook 或授权码。同一内容同一目标成功后跳过；
目标改变或日报内容改变会产生新投递。同一天修改内容后会发一份新完整日报。
跨日发送会排除该目标已经收到的、标题和摘要没有改变的条目；因此 arXiv 的
72 小时查询窗口不会让同一论文连续推送三天。`--force` 可以显式重新发送。
邮箱成功不影响飞书重试。这里的 `success` 表示服务端接受请求，不保证用户实际阅读。

SMTP 多收件人部分接受、请求超时、服务端响应不确定、发送后崩溃均保留 `pending`。
定时任务和 `--force` 不自动重试这些记录；同一目标的新日报也会等待恢复，避免重复发送。查看收件箱或群聊并确认
没有工作进程正在发送后，显式恢复：

```bash
dailyinfo delivery-status
dailyinfo delivery-reset <完整的64位投递键>  # 会再次确认；仅将 pending 改为可重试
dailyinfo deliver
```

如果消息已经送达，执行 `dailyinfo delivery-confirm <完整的64位投递键>`，
确认后记为成功并释放该目标，后续日报可继续投递且不会重复推荐这些条目。
明确拒绝的请求记为 failed，下次 `deliver` 会重试。

按北京时间 08:00 触发的外部调度依次执行：

```bash
dailyinfo run
dailyinfo recommend --search-arxiv
dailyinfo deliver
```

调度器必须检查每步退出码，生成失败时停止本次投递，避免发送旧日报。
DailyInfo 不安装系统定时任务。首版不做即时热点通知、管理后台、点击反馈和全文语义事件聚合。
