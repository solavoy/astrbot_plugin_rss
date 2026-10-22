# RSS 插件架构

## 宿主接口参考

插件要求 AstrBot 4.29.0 或更新版本。管理页通过 AstrBot 已认证的 [Plugin View 接口](https://docs.astrbot.app/en/dev/star/guides/plugin-pages.html)注册 Web API；聊天消息使用实际事件的 `unified_msg_origin` 和 `Context.send_message`，按[官方发送接口](https://docs.astrbot.app/en/dev/star/guides/send-message.html)发出。

飞书复用 AstrBot Lark 适配器中已配置的客户端，接口参考：[消息发送](https://open.feishu.cn/document/server-docs/im-v1/message/create)、[图片上传](https://open.feishu.cn/document/server-docs/im-v1/image/create)、[文件上传](https://open.feishu.cn/document/server-docs/im-v1/file/create)和[卡片 JSON 2.0](https://open.feishu.cn/document/feishu-cards/card-json-v2-structure)。可选浏览器和 Telegraph 分别参考 [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) 与 [Telegraph API](https://telegra.ph/api)。

## 数据流

`Scheduler → Fetcher → Parser → content_blocks → Repository → 路径游标 → 唯一目标投递 → Renderer → Sender`。

Feed 为全局共享源，DirectSubscription 与 SubscriptionList/ListFeed/ListSubscription 独立引用。底层下载周期取活跃路径有效间隔的最小值；每条路径持久化自己的文章序号游标和 next_check_at。路径到期一次性将积累文章入队，在同一事务内推进游标。首次订阅先建立基线，初次 Feed 抓取也不投递旧文章（除显式启用历史）。

直接路径按订阅覆盖、Feed 覆盖、全局默认的顺序取周期；列表路径按订阅覆盖、列表覆盖、全局默认取周期。未设置周期的列表不会继承 Feed 的独立覆盖。两类路径均执行最小间隔限制，再共同决定底层 XML 下载需求。

正文只有 `Article.content_blocks` 一个权威表示，HTML/Markdown/纯文本由渲染器派生。块允许有序 children，保留段落内图文顺序。普通截取仅用于直接订阅。列表全量内容仍受平台硬限制。

SQLite WAL、外键、迁移、唯一约束保护任务。投递唯一键 `(target_id, article_id)`，target 身份由平台实例和最终会话组成。对于 `GroupMessage`，若真实事件提供非空 `get_group_id()`，则用它替代可能包含发言用户标识的 `get_session_id()` 作为目标路由和去重键，并作为 sender 的群 session ID；`unified_msg_origin` 始终保存事件原值用于 AstrBot 消息投递。其他消息类型使用原始 session ID，不拆解或猜测 UMO。列表投递先原子认领文章，再逐篇生成并持久化消息卡片 payload、稳定 UUID 和文章映射；已成功投递不重发。发送前失败可回退，发送超时或进程在 sending 阶段退出转为 unknown，默认不自动重发。

HTTP 会话复用，Feed 锁、域名节流、有限重试。公开 URL 校验、重定向逐跳验证与 DNS 固定防重绑定。浏览器默认关闭，仅显式允许的站点遇到明确浏览器需求时回退；交互验证码或访问拒绝停止升级。Cookie 与浏览器 profile 存于持久化数据目录，状态可查询，凭据不进入普通导出。

飞书使用 schema=2.0、body.elements 和 markdown。文章标题和作者/源/配置时区副标题位于卡片头部，时间按 `general.timezone` 换算（默认 `Asia/Shanghai`）并以数字时间显示，原文地址以正文末尾文本链接呈现；列表中的每篇文章各自成卡。实际外层请求（content 字符串二次序列化）计 UTF-8 字节，硬限 30720，组件数也受控。图文文章中的图片上传到相同应用并使用返回的 image_key；文章含视频时，卡片后发送独立、持久化的 `media` 操作。纯视频文章不走飞书卡片，使用 AstrBot 默认 `Video.fromURL` URL 组件，由适配器下载并发送。媒体失败保留链接。Telegram 的 Telegraph 发布在渲染阶段，发送前失败可安全回退；Telegraph 只引用图片/视频原始 URL，不下载或上传媒体。

插件入口只负责生命周期和命令。WebAPI 与命令调用统一 Services；Web API 仅使用 AstrBot 已认证管理面板桥接。群内订阅操作按 `general.group_admin_only` 设置检查权限（默认允许群成员，启用后要求 AstrBot 管理员）；所有操作只接受事件真实身份，无法验证时拒绝。

## 生命周期

initialize 依次校验配置、打开插件数据目录数据库、恢复队列、构建客户端、注册管理 API、启动 scheduler/workers；terminate 取消并等待任务、关闭 browser/curl/其他网络客户端、最后关闭 SQLite。配置修改明确标注需重载，运行时暂停开关独立持久化。
