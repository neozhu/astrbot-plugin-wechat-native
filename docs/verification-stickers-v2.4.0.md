# 原生表情开发、验收与升级记录

基于 [SPEC v1.1](SPEC-WeChat%20Native%20Sticker%20Receive%20Save%20%20Send.md)，目标插件 v0.3.15。
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
| B6 管理员/原图/WXGF | 来源拒绝、明确请求识别、最终权限重查测试 |
| C1 Saved 查询 | `test_catalog_is_bounded_metadata_only_and_selection_is_event_bound` |
| C2 已添加官方包 | 同上；`test_membership_removed_after_find_prevents_post[pack]` |
| C3 saved 来源原生发送 | `test_native_send_reuses_outbox_and_sends_one_selected_original[saved]` |
| C4 pack 来源及单项按需获取 | 同上 `[pack]`，原图缺失仍提交该单项 Native send Job，不下载整包 |
| C5 严格成功回执 | `test_unconfirmed_send_never_reports_success_or_submits_twice` 六种故障；原子回执测试 |
| C6 重复工具/断线/未知结果 | 同上；并发发送、重启恢复、延迟确认循环保护测试 |
| D1 默认关闭兼容 | `test_sticker_switches_and_existing_access_policy`、工具默认关闭零 HTTP 测试 |
| D2 @/白名单/循环/管理员 | 同上；管理员请求与撤权测试、群限流/撤回测试、原有 loop/mention 测试 |
| D3 既有功能回归 | 全量 pytest，保留原有文本、图片、语音、PDF、引用、撤回与连接测试 |

TDD 每阶段的失败、实现和通过记录见[实施计划证据表](superpowers/plans/2026-11-14-stickers.md)。
原始运行日志位于工作区 `.scratch/stickers/`（不纳入发布包）。

## 最终自动化结果

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
- [ ] 首次收藏后，在手机微信 Saved 中确认出现。
- [ ] 再次收藏相同字节内容，返回已收藏且手机没有重复项。
- [ ] 从个人 Saved 发送，在接收手机确认显示为原生表情。
- [ ] 从已添加官方包发送，包含缺少本地原图时的单项按需获取。
- [ ] 模拟断线、超时与重启，检查手机和 Native Job，确认没有重复收藏/发送。

## 部署升级

1. 停止 AstrBot 机器人，备份原插件数据目录（含 SQLite、WAL/SHM）和 Native 数据。
   保留原 `server_url`、`account_id` 与机器人 ID，避免切换到另一份空状态库。
2. 将 Native 镜像升级为 `ghcr.io/thisnick/agent-wechat-native:v2.4.0`，确认账号在线。
3. 更新插件至 v0.3.15 并安装 `requirements.txt`，Python 3.12+、AstrBot 版本范围不变。
4. 沿用 `token_file` 的目标账号 `read + media + send` scopes；无需额外 Admin Token。
   需要收藏时，将管理员真实 senderId 加入已有 `wechat_action_admins`。
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
- 明确收藏使用保守的常用请求句式校验，例如“请收藏这个表情”“把这个表情收藏一下”；
  描述、疑问或否定句不会授权写入，复杂措辞可改用上述明确句式。
- 本地审计不是永久 Saved 事实。新触发请求重新读取 Native 目录；同一触发消息只查看原结果。
- 未知回执持续阻塞是有意行为。独立副本/状态库之间不共享 SQLite 唯一约束，
  部署时不要用多个独立机器人副本并发处理同一账号的同一事件流。

## 官方契约来源

- [Native v2.4.0 Sticker API](https://github.com/thisnick/agent-wechat/blob/v2.4.0/docs/src/content/docs/reference/stickers.mdx)
- [OpenAPI](https://github.com/thisnick/agent-wechat/blob/v2.4.0/interfaces/http/openapi.json)
- [收藏 Job](https://github.com/thisnick/agent-wechat/blob/v2.4.0/packages/native-client-rust/crates/wechat-core/src/sticker_jobs.rs)
- [原生发送及单项获取](https://github.com/thisnick/agent-wechat/blob/v2.4.0/packages/native-client-rust/crates/wechat-core/src/sticker_send.rs)
