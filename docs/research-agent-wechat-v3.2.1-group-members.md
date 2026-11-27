# Native v3.2.1 群成员兼容性核查

核查日期：2026-11-27。上游固定为 `thisnick/agent-wechat` 的 `v3.2.1`（`807044926db334f38f09cffc280f6a72cd5d0a3a`），直接比较基线为 `v3.2.0`（`2be4fad0fffe957cc7d669f5295c1d4377d60aec`）。本笔记基于 GitHub Release、标签源码和 PR diff；未连接真实微信账号。[发布标签](https://github.com/thisnick/agent-wechat/tree/v3.2.1)、[版本比较](https://github.com/thisnick/agent-wechat/compare/v3.2.0...v3.2.1)

## 上游结论

**v3.2.1 的群成员改动无需插件适配。** v3.2.0 和 v3.2.1 的 `/v1` OpenAPI 文件逐字节相同。与本插件 README 使用的 v2.9.0 相比，群管理、成员分页、单成员查询、主动刷新、操作回执和发送的路由定义，以及群成员相关 schema 都相同。`Keep/Clear/Set` 用于上游 Rust Profile 投影。[v2.9.0 OpenAPI](https://github.com/thisnick/agent-wechat/blob/v2.9.0/interfaces/http/openapi.json)、[v3.2.0 OpenAPI](https://github.com/thisnick/agent-wechat/blob/v3.2.0/interfaces/http/openapi.json)、[v3.2.1 OpenAPI](https://github.com/thisnick/agent-wechat/blob/v3.2.1/interfaces/http/openapi.json)、[PR #732](https://github.com/thisnick/agent-wechat/pull/732)

## 与群成员有关的变化

| 项目 | 上游变化 | 对 HTTP 消费者的影响 |
| --- | --- | --- |
| 新 Profile 的成员列表补齐 | 后台每轮最多刷新 5 个群；入队和还有到期任务时唤醒账号循环；各次元数据请求释放账号写锁；失败群独立退避，避免阻塞正常群。代码设置前两次失败分别等待 5 秒和 10 秒，第三次失败转入 held 并发出 `group.refresh_blocked`。 | 升级 Daemon 后生效；不能将“补齐更快”理解为读接口变成实时完整名册。[PR #737](https://github.com/thisnick/agent-wechat/pull/737)、[v3.2.1 刷新源码](https://github.com/thisnick/agent-wechat/blob/v3.2.1/packages/native-client-rust/crates/wechat-core/src/incoming_groups.rs) |
| 群名片清空 | 对本次实际返回的成员，省略的 display name 和 opaque 字段会清除原保存值；delta 中未返回、但仍在完整身份列表里的成员保留原字段；未知 protobuf 扩展保留。 | `roomDisplayName` 可能从旧名片变成 `null`；展示名和 @ 名称应使用当前响应的回退值，不能以空值为理由保留旧名片。[PR #732](https://github.com/thisnick/agent-wechat/pull/732)、[metadata 投影源码](https://github.com/thisnick/agent-wechat/blob/v3.2.1/packages/native-client-rust/crates/wechat-core/src/metadata.rs) |
| 清空自己的群名片 | `MyName` 清空时，本地省略对应 protobuf field 2；内部 `NativeMemberPatch` 的字段由 `Option<T>` 改为 `NativeFieldPatch<T>`（`Keep`、`Clear`、`Set`）。 | 这是 Daemon/Profile 侧持久化修复；HTTP 插件不直接操作这些 Rust 类型或 `chat_room.ext_buffer`。[PR #732](https://github.com/thisnick/agent-wechat/pull/732)、[Profile 投影源码](https://github.com/thisnick/agent-wechat/blob/v3.2.1/packages/native-client-rust/crates/wechat-profile/src/projection.rs) |
| 刷新循环与数据增长 | 群资料刷新期间又发生变化时，旧响应不再以队列任务的形式不断派生并重复刷新；旧副本由保留策略清理。 | 属于 Daemon 修复，无需插件增加轮询或自行维护刷新队列。[PR #726](https://github.com/thisnick/agent-wechat/pull/726)、[v3.2.1 发布说明](https://github.com/thisnick/agent-wechat/releases/tag/v3.2.1) |

## 仍需保留的读取语义

单个成员查询仍读取保存的 catalog：找到时 `state = present`，未找到时 `state = unconfirmed`，`coverage = cached`。源码明确说明缓存名册可能不完整，因此查不到不能证明该用户已经退群。[成员查询源码](https://github.com/thisnick/agent-wechat/blob/v3.2.1/packages/native-client-rust/crates/wechat-core/src/pagination.rs#L46-L73)、[HTTP 契约](https://github.com/thisnick/agent-wechat/blob/v3.2.1/interfaces/http/routes.txt)

成员分页沿用原响应格式。`roomDisplayName` 是可空的群名片字段，保存成员的搜索也使用当前的群名片、昵称等字段。[分页序列化源码](https://github.com/thisnick/agent-wechat/blob/v3.2.1/packages/native-client-rust/crates/wechat-core/src/pagination.rs#L543-L546)、[搜索字段](https://github.com/thisnick/agent-wechat/blob/v3.2.1/packages/native-client-rust/crates/wechat-core/src/pagination.rs#L598-L607)、[HTTP 契约](https://github.com/thisnick/agent-wechat/blob/v3.2.1/interfaces/http/routes.txt)

PR #732 的上游新增测试覆盖：清空成员名片后 `roomDisplayName = null`，`displayName` 和 `mentionName` 回退到昵称；未变化的 delta 成员保留原群名片；从完整名册移除的成员查询变为 `unconfirmed`。这些是已阅读的上游测试断言，本次未运行上游 Rust 测试。[新增测试](https://github.com/thisnick/agent-wechat/pull/732/files)、[标签化测试源码](https://github.com/thisnick/agent-wechat/blob/v3.2.1/packages/native-client-rust/crates/wechat-core/src/incoming_groups.rs)

## 插件影响

- **查成员和排行榜。** 成员搜索从 Native 读取当前群的保存名单，允许名称字段为空。排行榜依次使用群名片、`displayName` 和缩写后的成员 ID。升级后，已经清除的旧群名片可能搜不到，排行榜会显示回退名称。[成员查询](../history_tools.py#L52)、[排行榜](../group_ranking.py#L66)、[排行榜名称回退](../group_ranking.py#L174)
- **发送 @。** 插件按 `username` 核验群成员。成员资料缺失或未确认时，插件主动刷新并查询回执，再次确认成员后才发送显式 @。群名片为空不影响这项检查。[显式 @ 工具](../mention_tools.py#L15)、[成员检查和刷新](../native_client.py#L2329)
- **修改机器人自己的群昵称。** 现有工具只接受非空昵称，仍用 `PUT /my-name` 提交 `displayName`。上游修正了清空昵称后的本地存储，插件支持发起清空属于独立的功能需求。[工具参数](../main.py#L168)、[参数校验](../wechat_actions.py#L246)、[Native 请求](../native_client.py#L951)

## 验证范围

已核对发布说明、相关 PR 和三个标签的 OpenAPI。插件现有群成员查询、排行榜、@ 和群昵称相关测试共通过 76 项，均使用模拟 Native HTTP。未启动 v3.2.1 Daemon，未运行上游 Rust 测试，未使用真实微信账号验收。
