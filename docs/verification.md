# 验证记录与边界

本文记录自动化检查环境、回归范围、当前结果和真实环境验收限制。所有检查均从仓库根目录执行。

## 本地自动化验证

开发环境为 Python 3.15.0、pytest 9.1.1、Ruff 0.17.0。运行环境的 `tzdata` 位于仓库 `.qa_deps`，pytest 临时目录也应放在该目录下；每次使用新的 basetemp 名称：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) ".qa_deps")
$runId = "pytest-" + [guid]::NewGuid().ToString("N")
python -m pytest -q --basetemp (Join-Path ".qa_deps" $runId)
ruff check .
ruff format --check .
git diff --check
```

自动化回归覆盖 RSS/Atom/XML 解析、危险地址和重定向校验、抓取调度与共享源去重、订阅和投递状态持久化、失败恢复、管理 API，以及媒体与 AI 翻译渲染。翻译用例覆盖提供商未配置时不调用模型、提示词与 RSS 内容分离、目标语言跳过协议、混合语言段落处理、失败回退原文、提示词隔离缓存及源文章不被修改。媒体用例覆盖图文 RSS 中重复的 `View image` 链接和缩略图清理、纯视频路由，以及关闭媒体发送时保留链接并跳过插件下载。

按源翻译回归覆盖关闭开关时保留原文且不调用模型、共享列表中只翻译开启的源，以及翻译缓存复用；关闭源后即使已有缓存也仍返回原文。本地 UI harness 验证了开关默认关闭、开启并保存后列表显示开启、重新进入详情仍保持开启、关闭后状态恢复，以及新建源可开启翻译且成功后表单重置为关闭；这些交互使用内存假数据，不涉及真实平台。

回归用例覆盖插件元数据中的 `telegram` 与 `lark` 平台声明，以及 Discourse/Linux.do RSS 自动生成标题锚点的清理；标题文字、普通 hash 链接和跨站同形链接会保留。

## 本地集成核对

本地集成测试使用 fake AI/provider 和文章数据，确认配置提示词作为模型系统提示，RSS 标题和正文作为独立用户输入；默认目标语言为 `[简体中文]`，替换为 `[英语]` 后目标随之改变。已是目标语言的标题或段落只保留一次；不同提示词使用独立缓存，相同提示词可复用缓存。测试未调用真实 AI 服务。

集成测试确认启用 `media.send_as_media` 时，图文进入原有渠道渲染，纯视频交给 AstrBot 默认 `Video.fromURL` 组件并由适配器下载发送；关闭时显示链接且不触发插件媒体下载。飞书卡片复用 AstrBot 已配置的 Lark client，图文重复链接和关联缩略图会清理。此处使用本地 fake/桩接口，没有发送真实平台消息。

Linux.do 的标题锚点经解析和旧缓存清理后，在默认渲染、飞书渲染和 Telegraph 节点中均不再输出锚点 URL 或 `¶`，标题文本及正常站内/跨站链接保留；缓存清理不修改原文章对象或其 hash。检查使用本地数据和桩接口。

## 当前最终结果

2026-10-22 全量检查为 `346 passed, 7 warnings`。其中 6 条为 Windows Proactor/curl_cffi selector 提示，1 条为 feedparser 弃用提示。`ruff check .` 通过，`ruff format --check .` 确认 89 个文件已格式化，`node --check views/rss/app.js` 与 `git diff --check` 通过。

## 尚未覆盖的真实环境

当前记录不代表已在真实 AstrBot 实例、AI 提供商或飞书应用上完成端到端验收。未使用真实平台凭据，也未发送真实消息；适配器版本差异、实际权限、网络与平台侧媒体限制仍需在目标部署环境验证。默认 AstrBot URL 视频组件由宿主适配器负责下载上传，本插件的媒体下载器大小限制不适用于该默认视频路径。
