# RSS 订阅推送

在 AstrBot 中订阅 RSS/Atom 并定时推送。支持 Telegram 和飞书、共享订阅列表、图文和视频内容，以及按订阅源启用 AI 翻译。

## 安装

需要 AstrBot 4.29.0 或更新版本。在 AstrBot 插件目录运行：

```sh
cd AstrBot/data/plugins
git clone https://github.com/solavoy/astrbot_plugin_rss.git
```

安装后重启 AstrBot，在插件管理中启用“RSS 订阅推送”，并从控制台打开 RSS 管理页。

## 使用

在 RSS 管理页添加订阅源、管理共享列表、预览文章和查看推送记录。首次订阅只建立新文章基线，不补发已有文章。

订阅正文在解析时由 [markdownify](https://pypi.org/project/markdownify/) 从 XML HTML 转换为 Markdown，再由 [markdown-it-py](https://markdown-it-py.readthedocs.io/) 提供 Markdown token；正文以单个 `markdown` 内容块保存，默认渠道直接渲染 Markdown，Telegraph 按 token 生成其支持的节点。旧版内容块只在读取兼容层转换，数据库 JSON 结构不变。

命令都以 `/rss` 开头：

| 命令 | 用途 |
|---|---|
| `/rss sub <源 ID、URL 或 /route>` | 订阅 RSS/Atom 源 |
| `/rss unsub <源数字 ID、URL 或 /route>` | 取消当前聊天的直接订阅 |
| `/rss sublist <列表 ID 或名称>` | 订阅共享列表 |
| `/rss unsublist <列表 ID 或名称>` | 取消列表订阅 |
| `/rss list`、`/rss feed` | 查看列表或订阅源 |
| `/rss mine`、`/rss status`、`/rss help` | 查看当前订阅、调度状态或帮助 |

相对路由以 `/` 开头，并使用插件设置中的 `rsshub.default_url_prefix`。全局设置修改后需重载插件；订阅源翻译开关保存后立即生效。

抓取间隔支持非负整数加可选的单一单位：`d` 天、`h` 时、`m` 分、`s` 秒，忽略大小写并允许空白；不带单位默认按秒。例如 `1d`、`2h`、`30m`、`45s`、`300`。不支持组合单位或小数。全局间隔最少 1 秒，订阅源和列表填写 `0` 时继承全局间隔。配置和管理页保留原始填写值，例如 `1h` 保存后仍显示 `1h`，仅在内部换算为秒数。

## AI 翻译

先在插件配置中选择翻译模型，再到“订阅源 → 源详情 → 订阅设置”勾选“AI 翻译”并保存。新源和升级后的已有源默认关闭；只有该源已开启翻译且已选择模型时才调用 AI，同一源的直接订阅和列表订阅都遵循此设置。

`translation.prompt` 是系统提示词，默认目标语言为 `[简体中文]`，可修改方括号中的语言。原文默认附在译文前，已是目标语言的标题或段落只保留一次；翻译失败时回退为原文。

翻译按 Markdown 段落处理。链接、媒体地址、内联代码和格式标记由模型协议保护；原文与译文配对时媒体和代码只保留一份。

## 文档导航

- [部署说明](docs/deployment.md)
- [架构](docs/architecture.md)
- [数据库](docs/database.md)
- [Web API](docs/web-api.md)

## 协议

本项目基于 [AGPL](https://github.com/AstrBot-Elementary-School/astrbot_plugin_rsshub/blob/master/LICENSE) 协议开源。

## 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [astrbot_plugin_rsshub](https://github.com/AstrBot-Elementary-School/astrbot_plugin_rsshub)。
