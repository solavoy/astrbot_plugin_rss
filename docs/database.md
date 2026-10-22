# SQLite 与一致性设计

数据库置于 `StarTools.get_data_dir("astrbot_plugin_rss")/rss.sqlite3`。启用 WAL、foreign_keys、busy_timeout，执行 user_version 迁移。Schema v3 为 `feeds` 增加 `translation_enabled`，默认关闭；从 v1/v2 升级的 Feed 均以关闭状态迁移。所有写入参数化；关键多步操作在锁保护的事务完成。

| 表                             | 作用与约束                                                                                          |
| ----------------------------- | ---------------------------------------------------------------------------------------------- |
| feeds                         | 全局 URL/路由唯一、抓取间隔覆盖、AI 翻译开关、HTTP validators、初始化基线、next_fetch、错误状态                               |
| articles                      | 自增 seq 作为路径游标，article_id 唯一，feed_id+entry_id 唯一，content_blocks JSON                            |
| targets                       | platform_instance+routing_key 唯一，routing_key 由实际会话类型和最终 session_id 组成；保留真实 unified_origin 用于发送 |
| direct_subscriptions          | target+feed 唯一、独立路径周期/游标                                                                       |
| subscription_lists            | 名称、启用状态、interval 覆盖                                                                            |
| list_feeds                    | list+feed 唯一；成员加入时记录基线，移除不删除 Feed                                                              |
| list_subscriptions            | target+list 唯一、独立路径游标与周期                                                                       |
| 路径游标                          | 合并在直接订阅、列表订阅和列表成员关系中，保存 baseline_seq、last_seq、next_check_at                                    |
| deliveries                    | target+article 唯一，pending/rendering/sending/success/failed/unknown，重试时间与归属路径                   |
| delivery_attempts             | 每次尝试结果、错误、平台消息 ID                                                                              |
| delivery_batches              | batch、稳定 UUID、卡片索引/总数、payload、发送结果                                                             |
| batch_articles                | batch 与 article/delivery 的唯一关联                                                                 |
| test_operations               | 人工测试推送独立操作，复用持久化卡片状态，独立于正常目标/文章去重                                                              |
| fetch_logs                    | curl/browser/304/错误和耗时，不含 Cookie                                                               |
| 站点会话文件                        | 受保护的独立 Cookie JSON 与浏览器 profile，位于抓取持久化目录；数据库导出不包含这些文件                                         |
| 媒体缓存文件 / platform_media_cache | 本地媒体与元数据文件；数据库保存应用实例维度上传 key 和有效期                                                              |
| telegraph_pages               | article/content_hash 对应发布 URL                                                                  |
| settings                      | 暂停等运行态                                                                                         |

## 游标与去重

文章 seq 由数据库分配。首次订阅对已有 Feed 从当前最高 seq 起步；未初始化的 Feed 首次成功抓取建立全体基线。列表新成员记录该 Feed 已有文章上界并跳过历史；慢路径查询 `seq > cursor AND seq > membership_baseline`。每个目标只创建一条投递，即使来自多个列表。

`feeds.url` 可保存完整 HTTP(S) 地址，或以单个 `/` 开头的规范化相对路由。相对路由在创建、导入或作为列表成员引用时仍以路由形式保存；抓取时按当前默认地址前缀构造有效 URL，配置的访问密钥只用于该请求，不写入 Feed 地址。导出保留相对路由，导入后仍按当前配置解析。

取消路径不会误删其他关系。认领或发送前重新确认文章仍被任一活跃路径覆盖；原路径失效时选择其他有效路径继续。成功/unknown 不允许普通重试按钮重发；unknown 需要人工核对后显式确认。

渲染任务可在重启时恢复 pending；sending 的结果不可推断，重启转 unknown。列表每张卡是独立持久化发送操作，稳定 UUID 不随重试改变。成功卡和文章保留成功，未发或明确失败卡按有限指数退避重试。

备份使用 SQLite backup API 得到一致快照；常规导出只包含订阅业务数据，过滤凭据。恢复需确认、暂停工作、校验 schema/integrity/外键并保留恢复前快照，不能直接执行用户上传 SQL。清理不能删除尚有待投递引用的文章或破坏慢路径积压。
