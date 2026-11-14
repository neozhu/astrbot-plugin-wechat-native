# SPEC — WeChat Native Sticker Receive, Save & Send

**版本：** v1.2（按用户追加要求调整收藏策略）

**目标插件：** v0.3.15（测试期间保持版本号）
**仓库：** `neozhu/astrbot-plugin-wechat-native`  
**基线：** `cb5e5c6`（v0.3.15，初版基线为 v0.3.14）
**Native：** v2.4.0  
**开发原则：** 复用现有架构、最小配置、TDD、避免重复操作

## 1. 开发目标

第一阶段仅完成两个功能。

### Feature A — 读取并收藏引用表情

支持私聊及群聊中引用别人发送的微信表情包：

- 读取引用表情的视觉内容，交给 AstrBot 当前视觉模型理解。
- 在允许群聊中，任何成员引用有效 Sticker 时自动保存到机器人微信账号的 Saved 收藏。
- 收藏不需要管理员、明确请求、@或 LLM 工具调用；私聊仅在调用工具时收藏，任何允许交互的用户均可使用。
- **收藏前自动去重，已经收藏的表情不再重复保存。**
- 下载失败、格式不支持或引用被撤回时，返回准确结果。

视觉问答遵守现有真实 `@机器人` 唤醒规则；群聊自动收藏静默执行，不唤醒模型。

### Feature B — 原生表情发送

支持两种来源：

- 个人 Saved 收藏。
- 微信账号已添加的官方表情包（Official Packs）。

AI 可根据用户请求或正常聊天语境选择合适的表情，不必每次明确说“发个表情”。

发送必须使用 Native Sticker API，不能将表情降级为普通图片发送。

## 2. 非目标

不实现自动视觉打标、Embedding 搜索、表情编辑器、批量导入、自动购买或添加官方表情包、独立管理 UI、跨账号同步及完整 GIF 动画理解。

不自动收藏直接发送的 Sticker 或私聊引用；只自动收藏允许群聊中的有效引用。
输入框里的小黄脸是文字表情，不作为 Sticker 图片下载或收藏。

## 3. 配置

仅增加：

```yaml
enable_stickers: false
```

默认关闭，保证升级兼容性。

复用现有：

- `enable_media`：引用表情视觉读取；群聊自动收藏由 `enable_stickers` 控制。
- `group_reply_mode`：群聊唤醒策略。
- `allowed_users` / `allowed_groups`：访问白名单。
- `wechat_action_admins`：仅用于现有群改名和撤回权限，不限制表情收藏。
- `token_file`：Native API 凭据。

不添加独立 Token、服务或复杂配置。

## 4. 引用表情读取

### 4.1 消息类型

识别：

| 类型 | Native 字段 |
|---|---|
| 普通表情 | `type=47` |
| 分享表情 | `type=49, appMessageType=8` |
| 引用回复 | `type=49, appMessageType=57` |

有效引用表情必须满足：

- `quote.type=47`，或 `quote.type=49` 且 `quote.appMessageType=8`。
- `quote.state=embedded`。
- `quote.embeddedSticker=true`。

不支持或已撤回的引用不进入视觉处理链。

### 4.2 媒体获取

引用表情的媒体资源属于**承载引用的回复消息**。

使用当前回复的 `messageId`：

```http
GET /v1/accounts/{accountId}/messages/{replyMessageId}/media
```

选择 `kind=sticker` 的资产。

必要时请求：

```http
POST /v1/accounts/{accountId}/messages/{replyMessageId}/media-transfers
```

```json
{
  "messageId": "REPLY_MESSAGE_ID",
  "assetId": "STICKER_ASSET_ID",
  "quality": "original"
}
```

通过已有 Job 轮询与媒体内容下载机制获取资源。

要求：

- 校验 `accountId`、`messageId`、`assetId`。
- 下载前后检查引用状态与撤回状态。
- 不直接请求微信原始 CDN URL。
- 不跨聊天查找或替换引用源。
- 优先使用 Native 已验证的视觉预览。

### 4.3 视觉交付

将有效 Sticker 预览转换为 AstrBot `Image` 组件。

GIF 可以提取代表帧供模型理解，但不能把静态帧分析声称为完整动画理解。

保留用户原始提问。

不需要新增独立视觉 AI 服务。

## 5. 收藏表情与去重

### 5.1 Tool

新增：

`wechat_save_quoted_sticker()`

不接受外部传入的 `messageId`、`assetId` 或目标账号。

从当前可信事件中确定引用来源。

所有允许交互的用户均可调用，无需管理员。群聊有效引用在实时消息接收后自动复用同一收藏实现，
不要求明确收藏请求、@或模型工具调用。仍检查群聊开关、白名单、循环保护、时间及撤回状态。
自动收藏结果只记录状态和安全日志，不自动向群内发送确认。

### 5.2 去重规则（重点）

**必须实现两层去重：内容去重 + 操作幂等。**

#### 第一层：内容去重

保存前：

1. 获取当前引用表情经过验证的原始资源。
2. 取得原始资源的规范化 Sticker 内容身份；对于 Native Saved 所使用的 32 位内容 ID，按其实际原图字节计算对应 MD5 身份。
3. 查询当前账号：

   ```http
   GET /v1/accounts/{accountId}/custom-stickers
   ```

4. 将待收藏的内容身份与 Saved 中的 `id` 比较。
5. 如果已存在，直接返回 `already_saved`，不调用保存 POST。
6. 如果不存在，再进入保存流程。

不得用文件名、发送者、消息 ID、预览图或文字描述作为内容去重依据。

原图无法验证时，不允许仅凭缩略图判断内容相同。

#### 第二层：并发及请求幂等

同一表情可能被不同群友引用，也可能同时触发多个收藏操作。

必须保证：

- 同一触发消息不会重复提交。
- 同一账号对同一内容身份的并发收藏最多只有一个在途操作。
- 通过 SQLite 原子约束或事务实现并发去重，不能仅使用内存锁。
- POST 使用稳定的 `Idempotency-Key`。
- 超时后只查询原操作，不更换 Key 重试。
- `needs_review` 的同内容操作保持阻塞，直到结果被确认。

即使插件预检查发生竞态，Native 自身的 Saved 状态协调仍作为最终防线。

#### 去重状态

| 状态 | 含义 | 行为 |
|---|---|---|
| `already_saved` | 内容已在 Saved 中 | 不提交 POST |
| `pending` | 同内容保存正在进行 | 查询或返回原操作 |
| `success` | 收藏成功并完成写回 | 返回成功 |
| `needs_review` | 远程结果未知 | 不允许再次提交 |
| `failed` | 已确认失败 | 返回失败原因 |

`already_saved` 属于正常成功结果，不记录为错误。

### 5.3 保存 API

必须使用已经下载并验证的原始 Sticker Asset：

```http
POST /v1/accounts/{accountId}/custom-stickers
Idempotency-Key: <STABLE_KEY>
```

```json
{
  "source": {
    "kind": "message",
    "messageId": "REPLY_MESSAGE_ID",
    "assetId": "VERIFIED_ORIGINAL_ASSET_ID"
  }
}
```

只允许保存 `kind=sticker`、`variant=original` 的可用资源。

不支持直接使用 WXGF 替代格式或缩略图收藏。

### 5.4 保存结果

Native Job 类型：`sticker_saved`。

完成条件：

- `state=available`
- `phase=completed`
- `result.profileWriteback=complete`
- `result.serverAcceptance=accepted` 或 `reconciled_noop`

`reconciled_noop` 表示目标收藏状态已经满足。

完成后刷新或失效 Saved 目录缓存。

## 6. 原生表情发送

### 6.1 来源 API

Saved：

```http
GET /v1/accounts/{accountId}/custom-stickers
```

官方表情包：

```http
GET /v1/accounts/{accountId}/sticker-packs?membership=added
GET /v1/accounts/{accountId}/sticker-packs/{packId}/stickers
```

只允许选择当前账号已添加的官方表情包。

缺少可用原始 GIF 时，可以通过 `POST /sticker-downloads` 按需获取选中的单个表情；不得自动下载整包。

### 6.2 LLM Tools

**`wechat_find_stickers(query: str = "", limit: int = 12)`**

- 从 Saved 和已添加官方表情包中查找候选。
- 优先使用官方 `caption` 与表情包名称。
- 返回有限候选列表。
- 不根据未知图片 ID 猜测含义。
- 不自动发送。

**`wechat_send_sticker(selection_id: str)`**

- 仅接受当前事件对应的候选。
- 再次验证账号、表情成员关系和当前聊天。
- 不允许自定义跨群发送目标。
- 发送原生 Sticker。
- 每条触发消息最多发送一个表情。

### 6.3 发送 API

Saved：

```json
{
  "kind": "sticker",
  "threadId": "CURRENT_CHAT_ID",
  "source": {
    "kind": "saved",
    "stickerId": "STICKER_ID"
  }
}
```

Official Pack：

```json
{
  "kind": "sticker",
  "threadId": "CURRENT_CHAT_ID",
  "source": {
    "kind": "pack",
    "packId": "PACK_ID",
    "stickerId": "STICKER_ID"
  }
}
```

发送接口：

`POST /v1/accounts/{accountId}/sends`

必须携带固定 `Idempotency-Key`。

### 6.4 发送成功条件

Native Job 类型为 `sticker_send`。

仅在以下条件成立时报告成功：

- `state=available`
- `result.serverAcceptance=accepted`
- 账号、聊天及 Job 身份验证通过。
- 存在有效 `serverMessageId`。

`queued/running` 不是成功。

`needs_review` 不允许重新发送。

发送频率默认固定：每条触发消息最多一个表情，同群主动表情间隔至少 30 秒。

## 7. SQLite 状态

新增一张轻量级操作表 `sticker_operations`，字段包括：

```text
account
thread
trigger_message_id
action
content_id
idempotency_key
source_hash
job_id
state
created_at
updated_at
```

必须建立：

- `(account, thread, trigger_message_id, action)` 唯一约束。
- 同一账号、同一 `content_id`、`action=save` 在 `pending/submitting/needs_review` 等在途状态下的唯一约束。

对 `content_id` 仍未知的保存请求，不允许进入 POST。

保存成功后可保留操作审计，但实际 Saved 集合仍以 Native 目录为准。

不得将本地记录当作永久收藏事实，因为用户可能从手机上删除收藏。

不存储原始媒体文件、聊天正文或 Token。

## 8. 主要改动文件

| 文件 | 变更 |
|---|---|
| `config.py` | 新增 `enable_stickers` |
| `adapter.py` | 引用表情识别、媒体交付、发送生命周期 |
| `event.py` | Sticker 预览转换为 Image |
| `native_client.py` | Sticker 媒体、收藏、目录和发送 API |
| `sticker_service.py`（新增） | 引用资源、收藏去重、目录和发送逻辑 |
| `sticker_tools.py`（新增） | 三个 LLM Tools |
| `main.py` | Tool 注册 |
| `state.py` | Sticker 操作记录与唯一约束 |
| `tool_support.py` | 权限与可信事件验证 |
| `README.md`、`docs/features.md` | 使用文档 |
| `tests/` | 精简核心测试 |

必要时可增加 Pillow 用于 GIF 静态预览转换，但不得建立独立的媒体处理服务。

## 9. 核心测试（精简为 20 项）

### A. 引用表情（5 项）

- [ ] 普通 Sticker `type=47` 与分享 Sticker `type=49/8` 均可识别。
- [ ] 引用表情使用当前回复 Message ID 获取媒体。
- [ ] 合法预览成功交付 AstrBot Image。
- [ ] 不支持、过期、已撤回资源正确处理。
- [ ] 跨账号、跨聊天或媒体身份不匹配时拒绝读取。

### B. 收藏与去重（6 项）

- [ ] 首次收藏成功并确认 Saved 写回。
- [ ] **相同表情已经收藏时返回 `already_saved`，且不提交 POST。**
- [ ] **不同引用消息指向相同内容时正确去重。**
- [ ] **并发收藏同一表情时最多一个在途 POST。**
- [ ] 同一请求重复执行、超时或重启不会重新提交未知操作。
- [ ] 普通群成员无需 @即自动收藏；越权聊天、无有效原图或 WXGF 不支持来源被拒绝。

### C. 原生发送（6 项）

- [ ] Saved 目录查询正常。
- [ ] 已添加官方表情包查询正常，未添加表情包不得发送。
- [ ] Saved 表情使用 `source.kind=saved`。
- [ ] 官方表情使用 `source.kind=pack`，必要时按需获取单个原始资源。
- [ ] `sticker_send` 的成功回执正确判断。
- [ ] 重复 Tool Calling、断线和未知发送结果不会产生二次发送。

### D. 回归与兼容（3 项）

- [ ] `enable_stickers=false` 时原功能行为不变。
- [ ] 视觉问答的群聊 @、白名单、循环保护有效；收藏不要求管理员。
- [ ] 原有文本、图片、语音、PDF、引用回复和撤回测试通过。

**测试策略：** 使用 pytest + Mock Native HTTP，覆盖上述 20 个关键场景。复用现有测试 fixtures，不为每一种错误状态建立重复测试文件。已有回归测试继续运行，不删除原有测试。

## 10. 真实微信验收（6 项）

- [ ] 引用群友的动态表情并 @机器人，AI 能读取可用视觉内容。
- [ ] 任意群成员不 @引用该表情，手机微信 Saved 中自动出现。
- [ ] **再次收藏相同表情，返回已收藏，Saved 中没有重复项。**
- [ ] 从个人 Saved 发送原生表情成功。
- [ ] 从已添加的官方表情包发送原生表情成功。
- [ ] 模拟断线/超时后，不发生重复收藏或重复发送。

## 11. 开发阶段

**Phase 1 — 引用读取**

完成 Native Sticker 资产读取、身份验证与 AstrBot Image 交付。

**Phase 2 — 收藏与去重**

完成内容身份检测、Saved 列表比对、SQLite 并发去重、保存 Job 与恢复。

**Phase 3 — 原生发送**

完成 Saved/Official Pack 查询、LLM Tools、发送 Job、幂等与限流。

**Phase 4 — 回归与发布**

执行自动测试、静态检查、真实微信验收与文档更新。

```bash
python -m pytest -q
ruff check .
ruff format --check .
python -m compileall -q .
```

## 12. Definition of Done

- 能理解群聊中被引用的可用表情。
- 能自动去重收藏允许群聊中任何成员有效引用的 Sticker。
- **相同内容不会重复收藏，并发与超时不导致重复写入。**
- 能从 Saved 和已添加官方表情包发送微信原生 Sticker。
- 所有写操作具备固定幂等键与可靠回执。
- 20 项核心测试及现有回归测试通过。
- 6 项真实微信验收完成，或明确列出受环境限制尚未验证的项目。
- 不引入额外服务和复杂配置。

## 13. Coding Agent 执行要求

直接基于当前仓库开发，不修改 Native 上游，不重构无关模块。

首先阅读 `adapter.py`、`event.py`、`native_client.py`、`state.py`、`tool_support.py` 和 `main.py`。

以 Native v2.4.0 官方契约为准，采用 TDD。优先完成引用读取，其次实现可靠收藏和内容去重，最后实现原生发送。

对收藏尤其注意：

1. 先验证原始 Sticker 资源。
2. 再比较当前 Saved 中的内容 ID。
3. 通过数据库约束防止并发重复提交。
4. 最后依靠 Native 幂等与状态协调确认真实结果。

不要增加标签管理、独立 UI、定时任务或无关功能。

完成后输出代码改动、20 项核心测试结果、已有回归测试结果、6 项真实验收结果及已知限制。

**Source of truth**

- https://github.com/thisnick/agent-wechat/blob/v2.4.0/docs/src/content/docs/reference/stickers.mdx
- https://github.com/thisnick/agent-wechat/blob/v2.4.0/docs/src/content/docs/how-it-works/message-types.mdx
- https://github.com/neozhu/astrbot-plugin-wechat-native
