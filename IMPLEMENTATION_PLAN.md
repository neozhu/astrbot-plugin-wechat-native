# WeChat Native Implementation Plan

目标：在仓库根目录独立实现可安装的 AstrBot Platform Adapter Plugin。
依据：用户需求、AstrBot v4.29.3 (f61a7f4)、agent-wechat v1.2.0 (33ec6b2)。
使用 aiohttp、Python 3.12、真实 AstrBot 类；无 Bridge、无 OpenAPI Key。

- [x] 1. 配置和 SQLite 状态：先测试 URL/Token 文件校验、白名单、原子游标与去重、重启后不确定状态保留，再实现 config.py/state.py。
- [x] 2. Native 客户端：先通过本地 aiohttp 服务测试 Bearer、路径编码、SSE 分帧/游标/错误、单次 POST + 幂等查询 + 最终回执，再实现 native_client.py。
- [x] 3. 平台与事件：先测试真实 AstrBot 注册、私聊/群 @/历史/撤回/隔离、回复和停止，再实现 adapter.py/event.py/main.py。
- [x] 4. metadata.yaml、依赖、中文 README 和 CI；运行完整 pytest、ruff、编译检查；独立代码审查并修复。

决策：直接在当前仓库实现，不另建项目或 worktree；tests/ 和 .github/workflows/ 为标准测试/CI目录。
安全边界：不接触真实 Token，不发真实消息；不复制第三方适配器代码，保留原 MIT 许可证。
可靠性边界：AstrBot 内部队列没有事务确认协议；崩溃前已交付队列的消息转 needs_review，禁止自动重放。保留消息标识和发送回执，不保存聊天正文。
评审重点：游标提交时序、跨账号/聊天身份、未知发送结果、撤回竞态、停止取消与资源释放。

验证记录：已完成独立代码审查；并发发送门控、撤回竞态、框架UUID、首启重试、服务身份绑定与消息身份复用均添加回归测试。
处理决策：Native 本地消息ID复用时保留游标并停止，要求人工检查；不自动替换旧输入身份。代价是该账号暂停接收直到管理员解决，避免旧任务对新消息产生副作用。

## v0.1.1 简单连接测试

- [x] 核对 AstrBot 4.29.3 Plugin Views、register_web_api 和 bridge 契约。
- [x] 先通过失败测试覆盖已保存机器人配置和只读 status → account 检测；实现 connection_view.py 与 main.py 路由。
- [x] 先通过失败测试覆盖页面四种状态、自动检测一次、手动重试和多机器人选择；实现 views/connection/ 静态卡片。
- [x] 更新中文说明、开发浏览器依赖和 CI，验证完整测试与静态检查。

验证：76 项测试通过（含 4 项 Chromium 页面交互测试），connection_view.py 覆盖率 100%；Ruff、格式、Python 编译及 JS 语法检查通过。独立审查未发现 Important 问题。

保持原配置表单和收发逻辑不变；不创建 StateStore，不发送消息，不返回 Token 或原始 Native 响应。
