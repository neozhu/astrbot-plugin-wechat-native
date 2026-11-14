"""Trusted tool boundary and safe JSON business results."""

import json
from dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import Platform

from .event import WeChatNativeEvent, session_id, session_target
from .native_client import NativeAPIError

MESSAGES = {
    "success": "操作已完成",
    "already_saved": "这个表情已在微信收藏中，无需重复收藏",
    "permission_denied": "当前没有权限执行这个操作",
    "not_available": "当前暂时无法使用这个功能",
    "configuration_error": "当前功能的权限配置尚未准备好",
    "invalid_request": "请求参数无效，请调整后再试",
    "not_in_group_chat": "这个功能要在群里使用",
    "invalid_cursor": "分页信息已失效，请重新查询",
    "ambiguous_target": "请明确要撤回哪一条消息",
    "already_recalled": "这条消息已经撤回",
    "not_recallable": "这条消息目前无法撤回",
    "rate_limited": "操作太频繁，请稍后再试",
    "pending": "操作已受理，仍在处理中",
    "needs_review": "操作结果暂时无法确认，请勿重复提交",
    "failed": "操作未能完成",
}

STICKER_ERROR_MESSAGES = {
    "sticker_quote_unavailable": "当前引用没有携带可读取的表情内容，请重新引用原表情发送。",
    "sticker_original_unavailable": "引用表情的可验证原图暂时不可用，尚未提交收藏。",
    "sticker_original_expired": "引用表情的原图已失效，请重新发送并引用。",
    "sticker_original_recalled": "引用表情或承载引用的消息已撤回，未提交收藏。",
    "sticker_original_unsupported_format": "引用表情的原图格式暂不支持收藏，未提交收藏。",
    "sticker_original_missing_scope": "读取引用表情的权限不足，请检查 Native 的媒体权限。",
    "sticker_original_too_large": "引用表情的原图超过处理限制，未提交收藏。",
    "sticker_selection_expired": (
        "表情候选来自之前的消息或已过期，尚未提交发送。"
        "请在当前请求中重新查找候选，再发送新的 selection_id。"
    ),
    "sticker_acquisition_failed": "选中表情的原始资源获取失败，尚未提交表情发送。",
    "sticker_acquisition_needs_review": (
        "选中表情的资源获取结果尚未确认，尚未提交表情发送；保留原回执，不重复获取。"
    ),
    "sticker_pack_original_unavailable": "选中表情尚无可验证的原始 GIF，尚未提交表情发送。",
}


class ToolError(Exception):
    def __init__(self, status, reason="business_state"):
        self.status = status
        self.reason = reason


def result(action, status, **fields):
    return {
        "ok": status in {"success", "already_saved"},
        "status": status,
        "action": action,
        "message": MESSAGES[status],
        "retryable": False,
        **fields,
    }


def encode(value):
    while True:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) <= 16384:
            return text
        if value.get("items"):
            value["items"].pop()
            value["count"] = len(value["items"])
            value["truncated"] = True
        else:
            return json.dumps(result(value["action"], "not_available"), ensure_ascii=False)


def is_tool_error(error):
    if isinstance(error, ToolError):
        return True
    error_type = type(error)
    initializer = error_type.__init__
    namespace = getattr(initializer, "__globals__", None)
    # AstrBot may replace the module object while an existing adapter retains
    # the old class. A matching name alone is not a trusted business exception.
    same_source_generation = (
        isinstance(namespace, dict)
        and namespace.get("__name__") == ToolError.__module__
        and namespace.get("ToolError") is error_type
        and getattr(getattr(initializer, "__code__", None), "co_filename", None)
        == ToolError.__init__.__code__.co_filename
    )
    return (
        error_type.__module__ == ToolError.__module__
        and error_type.__qualname__ == ToolError.__qualname__
        and (namespace is globals() or same_source_generation)
        and getattr(error, "status", None) in MESSAGES
    )


def error_result(action, error):
    reason = None
    if is_tool_error(error):
        status = error.status
        reason = error.reason
        logger.warning(
            "WeChat Native 工具拒绝：action=%s status=%s reason=%s", action, status, error.reason
        )
    elif isinstance(error, NativeAPIError):
        logger.warning(
            "WeChat Native 工具请求失败：action=%s http=%s code=%s",
            action,
            error.status,
            error.code,
        )
        status = (
            "invalid_cursor"
            if error.code == "invalid_cursor"
            else "configuration_error"
            if error.status == 401 or error.code in {"insufficient_scope", "forbidden"}
            else "permission_denied"
            if error.status == 403
            else "rate_limited"
            if error.status == 429
            else "not_available"
        )
    else:
        # No exception repr/trace: upstream errors can embed secrets or chat text.
        logger.warning("WeChat Native 工具失败：action=%s type=%s", action, type(error).__name__)
        status = "failed"
    if action in {"save", "send"} and reason in STICKER_ERROR_MESSAGES:
        output = result(action, status, reason=reason, message=STICKER_ERROR_MESSAGES[reason])
        if reason == "sticker_selection_expired":
            output["next_action"] = "wechat_find_stickers"
        return output
    return result(action, status)


@dataclass(frozen=True)
class ToolContext:
    event: object
    adapter: object
    account: str
    thread: str
    sender: str
    message_id: str


def _is_owned_reloaded_native_event(event, adapter, adapter_type):
    """Allow class identity drift only for a trusted event from an adapter generation."""
    if not isinstance(event, AstrMessageEvent) or not isinstance(adapter, Platform):
        return False

    event_type, actual_adapter_type = type(event), type(adapter)
    if (event_type.__module__, event_type.__qualname__) != (
        WeChatNativeEvent.__module__,
        WeChatNativeEvent.__qualname__,
    ) or (actual_adapter_type.__module__, actual_adapter_type.__qualname__) != (
        adapter_type.__module__,
        adapter_type.__qualname__,
    ):
        return False

    # Resolve the classes held by the adapter's own module generation.
    # A matching name alone must never authorize a tool invocation.
    factory = getattr(actual_adapter_type, "create_event", None)
    namespace = getattr(factory, "__globals__", None)
    if not isinstance(namespace, dict) or (
        namespace.get("WeChatNativeEvent") is not event_type
        or namespace.get("WeChatNativeAdapter") is not actual_adapter_type
    ):
        return False

    message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
    retained = getattr(adapter, "_events", None)
    return (
        isinstance(message_id, str)
        and bool(message_id)
        and retained is not None
        and retained.get(message_id) is event
    )


def is_native_event(event):
    from .adapter import WeChatNativeAdapter

    adapter = getattr(event, "adapter", None)
    return (
        isinstance(event, WeChatNativeEvent) and isinstance(adapter, WeChatNativeAdapter)
    ) or _is_owned_reloaded_native_event(event, adapter, WeChatNativeAdapter)


def context(event, *, write=False, group=False, send=False, stickers=False, sticker_recovery=False):
    adapter = getattr(event, "adapter", None)
    if not is_native_event(event):
        if not isinstance(event, WeChatNativeEvent):
            logger.warning(
                "WeChat Native 工具事件类型：actual=%s.%s expected=%s.%s",
                type(event).__module__,
                type(event).__qualname__,
                WeChatNativeEvent.__module__,
                WeChatNativeEvent.__qualname__,
            )
            raise ToolError("not_available", "event_type_mismatch")
        raise ToolError("not_available", "adapter_type_mismatch")
    raw = event.message_obj.raw_message
    cfg = adapter.native_config
    if stickers and (
        not cfg.enable_stickers or adapter._events.get(event.message_obj.message_id) is not event
    ):
        raise ToolError("not_available", "stickers_disabled_or_unowned_event")
    enabled = cfg.enable_wechat_actions if write else cfg.enable_history_tools
    if adapter.stopped or adapter._state_closed:
        raise ToolError("not_available", "adapter_stopped")
    if adapter.client.closed:
        raise ToolError("not_available", "native_client_closed")
    if not send and not stickers and not enabled:
        raise ToolError(
            "not_available", "wechat_actions_disabled" if write else "history_tools_disabled"
        )
    if not isinstance(raw, dict) or any(
        not isinstance(raw.get(k), str) or not raw[k]
        for k in ("id", "accountId", "threadId", "senderId")
    ):
        raise ToolError("invalid_request")
    thread, sender = raw["threadId"], raw["senderId"]
    expected_session = session_id(cfg.account_id, thread, sender if cfg.unique_session else None)
    try:
        valid = (
            raw["accountId"] == cfg.account_id
            and raw["id"] == event.message_obj.message_id
            and raw.get("direction") == "incoming"
            and sender == event.get_sender_id()
            and sender != adapter.bot_self_id
            and event.message_obj.self_id == adapter.bot_self_id
            and event.session.platform_id == adapter.meta().id
            and event.session.session_id == expected_session
            and session_target(event.session.session_id, cfg.account_id) == thread
            and (
                adapter.state.message_status(raw["id"]) in {"queued", "replied"}
                or (
                    stickers
                    and sticker_recovery
                    and adapter.state.message_status(raw["id"])
                    in {"needs_review", "partial", "failed"}
                    and adapter.state.sticker_operation(cfg.account_id, thread, raw["id"], "send")
                    is not None
                )
            )
            and adapter.state.message_matches(raw["id"], thread, raw.get("serverId"))
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid or raw.get("recall") is not None or event.is_stopped():
        raise ToolError("permission_denied")
    if adapter.loop_guard.blocked(thread):
        raise ToolError("not_available", "loop_cooldown")
    is_group = thread.endswith("@chatroom")
    if group and not is_group:
        raise ToolError("not_in_group_chat")
    if is_group:
        if cfg.group_reply_mode == "disabled" or (
            cfg.allowed_groups and thread not in cfg.allowed_groups
        ):
            raise ToolError("permission_denied")
        mentions = raw.get("mentions")
        if cfg.group_reply_mode == "mention" and not (
            isinstance(mentions, dict)
            and mentions.get("state") == "known"
            and mentions.get("self") is True
        ):
            raise ToolError("permission_denied")
    elif not cfg.enable_private_reply or (cfg.allowed_users and sender not in cfg.allowed_users):
        raise ToolError("permission_denied")
    return ToolContext(event, adapter, cfg.account_id, thread, sender, raw["id"])


def identity(item, ctx):
    if (
        not isinstance(item, dict)
        or item.get("accountId") != ctx.account
        or item.get("threadId") != ctx.thread
    ):
        raise ToolError("not_available", "message_scope_mismatch")


def page_args(limit, cursor):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ToolError("invalid_request")
    if not isinstance(cursor, str) or len(cursor) > 8192:
        raise ToolError("invalid_cursor")
