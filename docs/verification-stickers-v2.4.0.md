# 原生表情开发、验收与升级记录

基于 [SPEC](SPEC-WeChat%20Native%20Sticker%20Receive%20Save%20%20Send.md)，插件版本保持 v0.3.15。
初版的历史验证结果保留在下方；本次按用户要求将群聊引用改为自动去重收藏，
去掉收藏的管理员/明确请求限制，见[增量开发记录](superpowers/plans/2026-11-14-sticker-auto-save.md)。
最新全量回归 **916 passed**，95个 Sticker 专项场景通过；真实微信发送与自动收藏仍待验收。

## 2026-11-14 实机失败日志后的修复与诊断

用户确认 Native 为 v2.4.0，私聊和群聊都出现目录空、引用收藏失败。
提供的日志只证明 find 返回空候选，save 的 ToolError 被错误分类成泛化 failed；
不能据此断言此前收藏结果、手机库存或 Native 的具体故障原因。

- 已复现并修复：完整模块重导入时，新旧 ToolError 类属于不同模块命名空间，业务原因被吞掉。
  保留原有 importlib.reload 兼容及同名伪造异常拒绝测试。
- 引用未携带资源、撤回、原图过期、格式不支持、权限不足现在返回安全 reason 和具体提示。
- 接收和收藏时记录 quote_type、quote_state、embedded_sticker 及开关状态；不记录正文、CDN 或密钥。
- find 返回 Native Saved/已添加包/候选计数；空目录时只读查询 Native 运行版本。
  空列表不再被描述为收藏失败。未新增同步或下载 POST，幂等/未知结果规则保持不变。
- 全量回归 **896 passed**（68.25秒），静态检查与源码编译通过。版本保持 v0.3.15。

TDD 记录位于 `.scratch/stickers/`：reimport-error-red.txt（2 failed）到
reimport-error-green.txt（11 passed）；sticker-diagnostics-red.txt（5 failed）到
sticker-diagnostics-green.txt（87 passed）；missing-quote-red.txt（1 failed）到
runtime-diagnostics-full.txt（896 passed）。

更新代码后完整重启 AstrBot 进程/容器，确保微信适配器重新创建。
实机复测同一流程：私聊引用表情请求保存，再请求查找表情；收集新增的
“引用表情诊断”“表情目录诊断”和两个工具 JSON 结果，以继续定位 Native 资源/目录问题。
原始实机故障尚未确认恢复，不能把上述自动测试当作微信端验收通过。

11:07 复测仍返回不含 catalog 的旧 find 结果，故重新验证“插件更新后工具会使用新服务”这一前提。
本地用已保留的旧 StickerService 对象复现了相同旧格式；工具入口原先直接调用 adapter.stickers，
没有更新其类代际。现改为在可信工具入口替换旧实例，继续使用同一 Native 客户端、事件候选和 SQLite。
此修复不依赖用户反复重启来替换表情服务，不重新提交未知操作。
retained-service-red.txt：2 failed；retained-service-green.txt：90 passed（含原专项）；
retained-service-full.txt：全量 **898 passed**，68.41秒；静态、格式、编译和差异检查通过。
仍需实机确认新版 catalog/引用诊断出现后，再判断实际 Native 资源与目录是否可用。

11:14 实机目录查询已返回 saved_count=1、added_pack_count=0、candidate_count=1，
证明手动新增的 Saved 能被 Native 目录和插件读取。用户下一条消息请求发送时，模型复用了
上一条消息的 selection_id，因此被当前事件作用域校验拦截，尚未提交 Native 发送。
现提供 sticker_selection_expired 与 next_action=wechat_find_stickers，指导在当前请求查找新候选再发送；
工具说明要求明确发送请求直接串联 find→send，不因候选缺少 caption 额外确认。
已有发送意图优先只读核对，未知结果即使收到其他 ID 也不会引导重新发送。
stale-selection-red.txt：2 failed；stale-selection-green.txt：111 passed（含相关回归）。
stale-selection-full.txt：全量 **900 passed**，67.56秒；静态、格式、编译和差异检查通过。
真实微信发送成功仍待本次改动后的手机验收。

11:23 实机查询已返回 Saved=1、已添加包=2、候选=49，并在同一触发消息中找到“一二/哈哈”。
这次已进入发送入口，不能再归因于候选过期。核对 Native v2.4.0 `sticker_send.rs` 后确认：
Saved 可以在发送 Job 内按需获取 GIF；pack 来源缺少索引原图时直接拒绝
`sticker_source_unavailable`。此前把两者混为一谈，遗漏了 SPEC 的官方包单项获取。
日志本身未保留 Native 错误码，因此不能断言该条历史回执的具体拒绝原因。

- 官方包缺少原图时，固定 `packId + stickerId` 和幂等键提交一次 `/sticker-downloads`。
  SQLite 保存 `sticker_acquire` Job ID，再次调用、并发、跨消息和重启只核对原获取操作。
  获取完成后重新验证账号、包成员资格、原始 GIF 元数据及当前触发消息，再复用既有发送链路。
  获取失败或未知时不提交 `/sends`；恢复任务也不会自动补发此前未提交的表情。
- 已明确失败的发送在操作表/outbox 中均为 failed，不再误报 needs_review；
  记录经过白名单过滤的 HTTP/code，允许另发失败说明。同一 Sticker 意图仍不能重提。
  未知发送保留 needs_review 并静默停止后续回复，不再触发日志中的二次 TTS 发送异常。
- TDD：send-failure-red.txt（2 failed）→send-failure-green.txt（2 passed）；
  pack-acquisition-red.txt（12 failed）→pack-acquisition-green.txt（12 passed）；
  pack-acquisition-regression.txt（95 passed，含取消/只读恢复和跨连接获取去重）。
  pack-send-full.txt：全量 **916 passed**（71.43秒），唯一 warning 为既有 audioop 弃用提示。
  修改文件的 Ruff 检查、六个已规范格式文件的格式检查、源码编译和 git diff --check 均通过；
  native_client.py 沿用已确认的基线格式问题，只加入本次所需代码。

本轮改动文件：adapter.py、event.py、native_client.py、state.py、sticker_service.py、
tool_support.py、tests/test_stickers.py、docs/features.md、本验收记录及原开发计划。

本轮升级必须完整重启 AstrBot 进程/容器，让已有 NativeClient 加载新增的获取接口。
版本号仍为 v0.3.15，保留原状态库；历史未知请求不重提。发送一条新测试消息：
“找一个‘哈哈’表情包并发送”，手机确认原生表情显示后才算官方包发送验收通过。

11:42 实机已找到两个“开心”候选，但资源获取返回 `sticker_acquisition_needs_review`。
用户确认完整重启后仍复现；Native 容器输出只有 Daemon ready，尚无原始 Job/HTTP 错误证据。
当前不能把故障归因于热重载、Native 超时或具体 Job 契约不符，也不能宣称实机已修复。

- 单独复现了旧 NativeClient 不含新增获取方法时，尚未 POST 就错误留下未知意图的兼容缺陷。
  当前服务改为在原客户端/认证会话上执行最新获取包装方法，不重建发送架构或状态库。
  已有未知获取不因替换包装方法而重提。
- 获取诊断新增阶段、原回执键/Job ID、HTTP/安全错误码、Native state/phase/reason、
  acceptance 和身份匹配布尔值。缺失 Job ID、HTTP 失败、Native 待核对和身份不符可区分；
  不输出上游正文、账号内容、CDN 或密钥，不增加写请求。
- TDD：retained-client-red.txt（1 failed）→retained-client-green.txt（1 passed）；
  acquisition-diagnostics-red.txt（4 failed）→acquisition-diagnostics-green.txt（5 passed）；
  acquisition-diagnostics-regression.txt（113 passed，含原 Sticker 场景）。
- 待只读查看插件 `sticker_operations` 中原获取的 job_id；有 Job ID 时读取原 Job，
  没有 Job ID 时需结合 Native 持久记录核对，不删除记录、不换幂等键再次获取。

此轮仅修改 native_client.py、sticker_service.py、tests/test_tool_diagnostics.py 和本记录。
插件版本仍为 v0.3.15，状态结构和依赖不变。更新可增加诊断，但不解除历史未知意图；
引用收藏、Saved/官方包实际发送以及断线/重启去重仍待真实微信验收。

修改前工作区干净，`main` 为 `2188fc7`；与 `origin/main` (`95b132f`) 的差异只有 SPEC 文档。
已在修改前阅读六个指定实现文件，并核对 Native v2.4.0 的官方 OpenAPI 和服务端实现。

## 实现范围

- `config.py`：仅新增默认关闭的 `enable_stickers`，复用已有媒体和权限配置。
- `native_client.py`：Sticker 资产、验证内容、Saved/已添加包目录、收藏与原生发送 Job。
- `adapter.py`、`event.py`：引用预览交付，以及现有发送生命周期内的 Sticker 分支。
- `state.py`：`sticker_operations`、触发消息与同内容在途唯一约束、原子回执/outbox/消息写回。
- `sticker_service.py`、`sticker_tools.py`、`tool_support.py`、`main.py`：资源验证、去重、可信候选和三个工具。
- `tests/test_stickers.py`、`tests/test_tool_contract.py`：核心场景、故障恢复与真实 AstrBot 工具注册。
- `requirements.txt`：显式声明 Pillow；`README.md`、`docs/features.md`、`CHANGELOG.md`、`metadata.yaml`：用法及升级说明。

不新增发送架构、视觉服务、目录数据库、管理 UI 或定时同步。
保存目录每次从 Native 读取，因此没有需要失效的本地 Saved 缓存。
所有本次新增的异步写入在 POST 前持久化唯一意图与固定幂等键；回执未知时只读查询，禁止重提。
已确认发送在一个 SQLite 事务中完成操作、outbox 和原消息状态写回。

## 20项核心场景映射

以下均为 Mock Native HTTP/SQLite 自动化验证，不替代手机验收。参数化分支进一步覆盖故障边界。

| SPEC 项 | 自动测试证据 |
| --- | --- |
| A1 普通/分享表情识别 | `test_quoted_sticker_uses_reply_media_and_delivers_image` 两种引用类型 |
| A2 使用当前回复 ID | 同上，断言 `/messages/m1/media` 且不查引用服务器 ID |
| A3 AstrBot Image | 同上；`test_native_verified_preview_contract_is_preferred` |
| A4 无效、过期、撤回 | `test_invalid_quote_never_enters_visual_chain`、`test_quote_rejects_invalid_asset_without_download`、`test_invalid_sticker_frame_reports_unsupported_format` |
| A5 账号/聊天/媒体身份 | 资产拒绝测试、`test_save_denies_unauthorized_or_unverified_source[scope]`、下载中身份变更测试 |
| B1 首次收藏及写回 | `test_save_requires_confirmed_writeback_and_reuses_receipt`、`test_save_reconciled_noop_is_success_and_bad_job_identity_stays_blocked` |
| B2 already_saved 无 POST | `test_already_saved_is_success_without_post_and_phone_deletion_is_observed` |
| B3 不同引用同内容 | 同上，m1/m2 指向相同实际字节身份 |
| B4 并发同内容 | `test_sqlite_claim_serializes_same_content_across_connections`，两个独立 SQLite 连接 |
| B5 重复/超时/重启 | `test_concurrent_save_and_lost_admission_never_resubmit_after_restart`、`test_receipts_recover_after_restart_using_reads_only` |
| B6 普通用户/原图/WXGF | `test_save_tool_accepts_any_user_without_media_preview`、`test_save_denies_unverified_source`、自动收藏作用域重查测试 |
| C1 Saved 查询 | `test_catalog_is_bounded_metadata_only_and_selection_is_event_bound` |
| C2 已添加官方包 | 同上；`test_membership_removed_after_find_prevents_post[pack]` |
| C3 saved 来源原生发送 | `test_native_send_reuses_outbox_and_sends_one_selected_original[saved]` |
| C4 pack 来源及单项按需获取 | `test_missing_pack_original_acquires_only_selected_item_before_send`；获取失败/未知、元数据不符、权限变化均无发送 POST |
| C5 严格成功回执 | `test_unconfirmed_send_never_reports_success_or_submits_twice` 六种故障；原子回执测试 |
| C6 重复工具/断线/未知结果 | 同上；并发发送、重启恢复、延迟确认循环保护测试 |
| D1 默认关闭兼容 | `test_sticker_switches_and_existing_access_policy`、工具默认关闭零 HTTP 测试 |
| D2 @/白名单/循环/普通成员 | 自动收藏无需 @/管理员，视觉问答仍需 @；群限流/撤回、原有 loop/mention 测试 |
| D3 既有功能回归 | 全量 pytest，保留原有文本、图片、语音、PDF、引用、撤回与连接测试 |

TDD 每阶段的失败、实现和通过记录见[实施计划证据表](superpowers/plans/2026-11-14-stickers.md)。
原始运行日志位于工作区 `.scratch/stickers/`（不纳入发布包）。

## v0.3.15 初版自动化结果（历史）

验证环境使用仓库现有 `.venv/Scripts/python.exe`（Python 3.12），以及固定的 ruff 0.15.7。

| 检查 | 结果 |
| --- | --- |
| 修改前全量 `python -m pytest -q` | 813 passed |
| 最终全量 `python -m pytest -q` | **874 passed**，67.63秒；包含全部原有回归测试 |
| Sticker 与工具契约专项 | **76 passed**（57个 Sticker 参数化场景，19个工具契约场景） |
| `ruff check .` | 通过 |
| 源码 `compileall` | 通过；排除 `.venv/.git/.scratch/dist/data/__pycache__` |
| `git diff --check` | 通过 |
| 新增/本次格式化代码文件 `ruff format --check` | 9个文件通过 |
| 全仓 `ruff format --check .` | 未通过：8个文件存在基线格式问题，未做无关清理 |

全仓格式失败文件：`connection_view.py`、`main.py`、`native_client.py`、
`tests/test_connection_recovery.py`、`tests/test_connection_ui.py`、
`tests/test_connection_view.py`、`tests/test_group_ranking.py`、`tests/test_mentions.py`。
均已用 `git show HEAD:<path>` 输入同版本 formatter 复核基线也失败。
`main.py`、`native_client.py` 的无关格式保持原样，新功能代码按项目格式编写。
唯一 pytest warning 来自现有 AstrBot 依赖对 Python `audioop` 的弃用提示。

独立只读审查的三项发现均通过 TDD 修复：原子回执/状态提交、恢复发送的循环计数、
最后一次异步读取后的收藏权限核验。没有遗留审查发现。

## 真实微信验收：六项均待执行

本次没有连接已登录的真实微信账号或使用手机检查，不能将 HTTP 模拟测试记为实机成功。

- [ ] 引用群友动态表情并真正 @机器人，确认当前视觉模型能读取预览。
- [ ] 任意群成员不 @引用真实 Sticker，确认自动出现在机器人手机微信 Saved 中。
- [ ] 再次收藏相同字节内容，返回已收藏且手机没有重复项。
- [ ] 从个人 Saved 发送，在接收手机确认显示为原生表情。
- [ ] 从已添加官方包发送，包含缺少本地原图时的单项按需获取。
- [ ] 模拟断线、超时与重启，检查手机和 Native Job，确认没有重复收藏/发送。

## 部署升级

1. 停止 AstrBot 机器人，备份原插件数据目录（含 SQLite、WAL/SHM）和 Native 数据。
   保留原 `server_url`、`account_id` 与机器人 ID，避免切换到另一份空状态库。
2. 将 Native 镜像升级为 `ghcr.io/thisnick/agent-wechat-native:v2.4.0`，确认账号在线。
3. 更新插件的 `main` 分支代码并重启机器人；测试期间版本号保持 v0.3.15。
   本轮新增 NativeClient 单项获取方法，需完整重启 AstrBot 进程/容器，不能仅热重载插件。
   已部署 v0.3.15 的依赖不变，Python 3.12+、AstrBot 版本范围不变。
4. 沿用 `token_file` 的目标账号 `read + media + send` scopes；无需额外 Admin Token。
   收藏不需要配置 `wechat_action_admins`；该名单继续控制原有群改名和撤回工具。
5. 启动时自动追加轻量级操作表和索引；不删除旧表，不重新投递旧消息。先保持默认关闭，
   再显式开启 `enable_stickers`。引用读取还需 `enable_media` 和支持视觉的模型。
6. 执行上述六项手机验收后再推广。回滚插件时仍保留完整状态库和 Native 数据，
   不通过删库、更换机器人 ID 或新幂等键规避未知结果。

`needs_review` 的处置：有 Job ID 时读取该账号 `/jobs/{id}`；发送还可通过原幂等键读取。
收藏/下载若连首次 Job ID 都未收到，v2.4.0 无对应按键查询接口，不能自动确认，也不会再次 POST。
应结合 Native 保留的记录、手机 Saved/聊天核对，由维护者确认原结果；本版不提供强制解锁工具。

## 已知限制

- GIF 只交付代表静态帧；不做完整动画理解。视觉预览最大1600万像素，沿用10 MiB媒体限制。
- 按官方 caption/包名子串查找；无描述的 Saved 不猜含义。最多读取前50个已添加官方包，
  返回1–12个候选；每个事件保留最近120个候选，5分钟后失效。
- 开启 `enable_stickers` 后，允许群聊的有效引用静默自动收藏，不受 @、管理员或媒体视觉开关限制。
  直接发送的 Sticker 和私聊引用不自动收藏；微信小黄脸文字别名不作为 Sticker 图片收藏。
- 本地审计不是永久 Saved 事实。新触发请求重新读取 Native 目录；同一触发消息只查看原结果。
- 未知回执持续阻塞是有意行为。独立副本/状态库之间不共享 SQLite 唯一约束，
  部署时不要用多个独立机器人副本并发处理同一账号的同一事件流。

## 官方契约来源

- [Native v2.4.0 Sticker API](https://github.com/thisnick/agent-wechat/blob/v2.4.0/docs/src/content/docs/reference/stickers.mdx)
- [OpenAPI](https://github.com/thisnick/agent-wechat/blob/v2.4.0/interfaces/http/openapi.json)
- [收藏 Job](https://github.com/thisnick/agent-wechat/blob/v2.4.0/packages/native-client-rust/crates/wechat-core/src/sticker_jobs.rs)
- [原生发送及单项获取](https://github.com/thisnick/agent-wechat/blob/v2.4.0/packages/native-client-rust/crates/wechat-core/src/sticker_send.rs)
- [官方包单项获取 Job](https://github.com/thisnick/agent-wechat/blob/v2.4.0/packages/native-client-rust/crates/wechat-core/src/sticker_pack_download.rs)
