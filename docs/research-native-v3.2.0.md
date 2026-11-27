# Native v3.2.0 兼容性核查

核查日期：2026-11-27。上游固定为 `thisnick/agent-wechat` 的 `v3.2.0`（`2be4fad0fffe957cc7d669f5295c1d4377d60aec`），比较基线为本项目文档使用的 `v2.9.0`。本笔记记录源码、接口快照和发布产物核查，未连接真实微信账号。

## 上游结论

**没有发现普通聊天 HTTP/SSE 接入必须修改的破坏性契约变化。** v3.2.0 的主要变化位于 Daemon 内部：账号请求、二维码和已保存设备登录、会话续期、长连接与通话信令，以及 CDN/其他媒体下载改用原生异步传输。发布说明明确保留请求、收据和重试行为；`bf35510` 的文件变更集中在 `wechat-core`，没有修改 HTTP 路由或持久化迁移。这是源码兼容性判断，不能替代真实账号验收。[v3.2.0 发布说明](https://github.com/thisnick/agent-wechat/releases/tag/v3.2.0)、[bf35510 完整提交](https://github.com/thisnick/agent-wechat/commit/bf35510da240d02b7ea7d2e5c3740b24bb18b162)

注意：PR #714 的正文描述的是堆叠工作的第一步，称新增 transport 尚无调用者；v3.2.0 发布说明同时收录了后续迁移提交。判断整个版本应以发布标签和具体提交为准，不能只依据 PR 正文。[PR #714](https://github.com/thisnick/agent-wechat/pull/714)、[v3.2.0 发布说明](https://github.com/thisnick/agent-wechat/releases/tag/v3.2.0)

## v2.9.0 → v3.2.0 接口与部署比较

| 核查项 | 结果与影响 | 一手依据 |
| --- | --- | --- |
| HTTP API | 两个标签的 OpenAPI 均为 API v1；路径从 97 增至 98，无删除，新增 `/v1/accounts/{a}/calls/tickets`。既有路径变化集中于通话媒体 WebSocket 的描述。普通消息、发送、登录、媒体、表情、群管理等接口未出现契约差异。 | [v2.9.0 OpenAPI](https://github.com/thisnick/agent-wechat/blob/v2.9.0/interfaces/http/openapi.json)、[v3.2.0 OpenAPI](https://github.com/thisnick/agent-wechat/blob/v3.2.0/interfaces/http/openapi.json) |
| SSE 事件 | 非通话 schema 的变化为 `NativeEvent.kind` 说明增加 `message.updated`；该字段仍是字符串。大型合并转发 `49:40` 首次下载通常在 `message.committed` 前完成，仅首次失败后重试补齐才发更新事件；语音流式状态变化也会发该事件。消费者是否需要补处理取决于自身消息更新逻辑。 | [接口快照](https://github.com/thisnick/agent-wechat/blob/v3.2.0/interfaces/http/routes.txt)、[SSE 文档](https://github.com/thisnick/agent-wechat/blob/v3.2.0/docs/src/content/docs/use/api.mdx)、[语音更新源码](https://github.com/thisnick/agent-wechat/blob/v3.2.0/packages/native-client-rust/crates/wechat-core/src/sync_projection.rs) |
| 通话 | v3.0.0 将通话 WebSocket 从协议 v2 升到 v3，使用二进制 `WXCM` 音视频帧；旧通话客户端会被拒绝。该破坏性变更不等同于整个 `/v1` HTTP API 升版。 | [v3.0.0 发布说明](https://github.com/thisnick/agent-wechat/releases/tag/v3.0.0)、[通话协议快照](https://github.com/thisnick/agent-wechat/blob/v3.2.0/interfaces/http/calls-websocket.txt) |
| 启动与配置 | CLI 接口快照的行差异仅涉及 `wx calls` 的视频/摄像头/静音选项；`wx serve`、`wx daemon` 启动契约无差异。`deploy/native/` 内 Dockerfile、Compose、systemd 和两个 TOML 配置样例在两标签间 Git blob SHA 全部相同。 | [v2.9.0 CLI](https://github.com/thisnick/agent-wechat/blob/v2.9.0/interfaces/cli/wx.txt)、[v3.2.0 CLI](https://github.com/thisnick/agent-wechat/blob/v3.2.0/interfaces/cli/wx.txt)、[v2.9.0 部署目录](https://github.com/thisnick/agent-wechat/tree/v2.9.0/deploy/native)、[v3.2.0 部署目录](https://github.com/thisnick/agent-wechat/tree/v3.2.0/deploy/native) |
| 持久化 | `interfaces/storage/account.txt` 与 `registry.txt` 在两个标签间各自 Git blob SHA 完全相同；账号数据库 schema 仍为 15，注册库仍为 4，没有新增声明迁移。这只证明所核查的 Daemon SQLite 契约相同，不应推广为所有 Profile/运行时字节格式可任意降级。 | [v2.9.0 存储快照](https://github.com/thisnick/agent-wechat/tree/v2.9.0/interfaces/storage)、[v3.2.0 存储快照](https://github.com/thisnick/agent-wechat/tree/v3.2.0/interfaces/storage) |
| 发布产物 | GitHub Release 已有独立 `wx-v3.2.0-*` 二进制、校验文件和桌面安装包；发布说明明确 npm 发布与部署是独立步骤，不能由 GitHub Release 推断 npm 已同步。npm 源码包仍为 `@agent-wechat/cli`，启动命令仍为 `wx`，Node 要求与 v2.9.0 相同，均为 `>=24.16`。 | [Release 资产](https://github.com/thisnick/agent-wechat/releases/tag/v3.2.0)、[v2.9.0 CLI 包](https://github.com/thisnick/agent-wechat/blob/v2.9.0/packages/cli/package.json)、[v3.2.0 CLI 包](https://github.com/thisnick/agent-wechat/blob/v3.2.0/packages/cli/package.json) |

本次还通过 GHCR bearer manifest 请求确认 `ghcr.io/thisnick/agent-wechat-native:v3.2.0` 和 `v2.9.0` 均返回 200；v3.2.0 manifest 包含 `linux/amd64` 与 `linux/arm64`，索引 digest 为 `sha256:41ce51d22df52e1d84a54dff0376c18219a2a5426986b9f6dc14ca41ee6919e3`。这是产物存在性检查，没有拉起容器。[上游镜像包](https://github.com/thisnick/agent-wechat/pkgs/container/agent-wechat-native)、[上游容器部署说明](https://github.com/thisnick/agent-wechat/blob/v3.2.0/docs/src/content/docs/zh-cn/setup/run.mdx)

## 插件影响与升级验收

- **沿用现有接入。** Python 客户端已经使用 `aiohttp.ClientSession` 请求 Daemon 并订阅 SSE。连接时校验账号和 `serverId`，首次绑定通过 `newest=true&limit=1` 获取初始游标，已有绑定用原游标续接。上游的语音解码和媒体传输修复随 Daemon 生效。[Native 客户端](../native_client.py)、[适配器](../adapter.py)、[v3.1.0 发布说明](https://github.com/thisnick/agent-wechat/releases/tag/v3.1.0)、[媒体迁移提交](https://github.com/thisnick/agent-wechat/commit/91f8056a72a2b3a9858354663c9b73745918b5a0)
- **媒体取消诊断可以单独增强。** 上游可将 `voice_cancelled` 和 `media_cancelled` 映射为 HTTP 410。用本地模拟服务给语音下载返回这两种响应时，当前客户端将两者降级为 `event_cursor_expired` 并报告媒体不可用。显式的 `message_recalled` 仍识别为撤回。如果要保留取消码，应同时调整媒体错误映射，避免仅添加白名单后落入 410 撤回分支并误记撤回。[上游 HTTP 错误映射](https://github.com/thisnick/agent-wechat/blob/v3.2.0/packages/native-client-rust/crates/wechat-api/src/lib.rs#L465-L481)、[Native 客户端](../native_client.py)、[适配器](../adapter.py)
- **按需处理 `message.updated`。** 当前适配器对它只推进游标。大型合并转发首次展开失败后补齐、语音流式状态变化，可能在入站后更新内容。如果需要支持这些场景，应按原消息标识更新已有记录，保留原入站和撤回规则，避免重复回复。[适配器](../adapter.py)、[SSE 文档](https://github.com/thisnick/agent-wechat/blob/v3.2.0/docs/src/content/docs/use/api.mdx)、[语音更新源码](https://github.com/thisnick/agent-wechat/blob/v3.2.0/packages/native-client-rust/crates/wechat-core/src/sync_projection.rs)
- **先验证 Daemon 升级，再调整文档版本。** 停止 Daemon 后备份数据目录、外置官方 Profile 和插件状态库，试升级时沿用原配置和数据卷。确认 `serverId` 一致，SSE 从原游标续接，文本收发及发送回执正常。再按已启用功能验证面板登录和图片、语音、表情。通过后可更新 README 的兼容版本和 Compose 示例。[项目运行说明](operations.md)、[上游存储与备份说明](https://github.com/thisnick/agent-wechat/blob/v3.2.0/docs/src/content/docs/zh-cn/how-it-works/storage.mdx)

## 验证范围

已核对 GitHub Release、PR、提交文件列表、标签化 OpenAPI/路由/CLI/存储契约和部署目录，并检查 GHCR manifest。媒体取消诊断通过本地模拟 HTTP 验证。尚未运行 v3.2.0 Daemon、核验 npm registry 或在真实微信账号上验收。此次交付为研究文档。
