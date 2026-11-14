import importlib
import json
from pathlib import Path

import pytest
from aiohttp import web
from astrbot.api import logger  # noqa: F401
from conftest import module
from test_history_tools import tool_event
from test_stickers import find, save, save_event, send_event


@pytest.mark.parametrize(
    "admins,expected", [([], "permission_denied"), (["user"], "ambiguous_target")]
)
async def test_recall_without_id_preserves_business_error_after_reload(
    serve, tmp_path, caplog, admins, expected
):
    adapter, event, _ = await tool_event(
        serve, tmp_path, enable_wechat_actions=True, wechat_action_admins=admins
    )
    plugin = object.__new__(module("main").WeChatNativePlugin)
    # Existing action instances keep the exception class imported before reload.
    importlib.reload(module("tool_support"))
    requests = []

    async def handler(req):
        requests.append(req.method)
        return web.json_response({"items": [], "nextCursor": None})

    adapter.client.api_url = await serve(handler)
    try:
        value = json.loads(await plugin.wechat_recall_own_message(event))
        assert value["status"] == expected
        assert value["ok"] is False
        assert requests == (["GET"] if admins else [])
        assert f"status={expected}" in caplog.text
    finally:
        await adapter.terminate()


def test_foreign_tool_error_is_not_trusted(caplog):
    support = module("tool_support")
    foreign_type = type("ToolError", (Exception,), {"__module__": support.__name__})
    error = foreign_type("synthetic-secret")
    error.status = "success"
    error.reason = "synthetic-secret"
    assert support.error_result("recall_own_message", error)["status"] == "failed"
    assert "synthetic-secret" not in caplog.text


@pytest.mark.parametrize("direction", ["old_error", "new_error"])
def test_business_errors_survive_full_module_reimport(caplog, direction):
    support = module("tool_support")
    namespace = {
        "__name__": support.__name__,
        "__package__": support.__package__,
        "__file__": support.__file__,
    }
    exec(
        compile(Path(support.__file__).read_text(encoding="utf-8"), support.__file__, "exec"),
        namespace,
    )
    old_error = support.ToolError("not_available", "sticker_original_unavailable")
    new_error = namespace["ToolError"]("not_available", "sticker_original_unavailable")
    handler, error = (
        (namespace["error_result"], old_error)
        if direction == "old_error"
        else (support.error_result, new_error)
    )
    assert handler("save", error)["status"] == "not_available"
    assert "reason=sticker_original_unavailable" in caplog.text


async def test_disabled_history_logs_runtime_reason_without_request(serve, tmp_path, caplog):
    adapter, event, _ = await tool_event(serve, tmp_path)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        result = json.loads(await plugin.wechat_get_recent_messages(event))
        assert result["status"] == "not_available"
        assert "reason=history_tools_disabled" in caplog.text
    finally:
        await adapter.terminate()


async def test_native_failure_logs_status_and_safe_code_without_body(serve, tmp_path, caplog):
    adapter, event, _ = await tool_event(serve, tmp_path, enable_history_tools=True)

    async def handler(req):
        return web.json_response(
            {"error": {"code": "not_found", "message": "synthetic-secret-and-chat-body"}},
            status=404,
        )

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        result = json.loads(await plugin.wechat_search_messages(event, "synthetic-query"))
        assert result["status"] == "not_available"
        assert "http=404 code=not_found" in caplog.text
        assert "synthetic-secret-and-chat-body" not in caplog.text
        assert "http" not in result and "code" not in result
    finally:
        await adapter.terminate()


async def test_foreign_event_logs_event_mismatch(caplog):
    plugin = object.__new__(module("main").WeChatNativePlugin)
    result = json.loads(await plugin.wechat_get_recent_messages(object()))
    assert result["status"] == "not_available"
    assert "reason=event_type_mismatch" in caplog.text


async def test_reloaded_tool_class_accepts_adapter_owned_event(serve, tmp_path, monkeypatch):
    adapter, event, msg = await tool_event(serve, tmp_path, enable_history_tools=True)
    support = module("tool_support")
    original_type = type(event)
    reloaded_type = type(
        "WeChatNativeEvent",
        (original_type,),
        {"__module__": original_type.__module__},
    )
    monkeypatch.setattr(support, "WeChatNativeEvent", reloaded_type)
    assert not isinstance(event, reloaded_type)

    async def messages(*args, **kwargs):
        return {"items": [msg], "nextCursor": None}

    adapter.client.messages = messages
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        result = json.loads(await plugin.wechat_get_recent_messages(event))
        assert result["ok"] is True
        assert result["count"] == 1
    finally:
        await adapter.terminate()


async def test_reload_fallback_rejects_unowned_event(serve, tmp_path, monkeypatch, caplog):
    adapter, event, _ = await tool_event(serve, tmp_path, enable_history_tools=True)
    support = module("tool_support")
    original_type = type(event)
    reloaded_type = type(
        "WeChatNativeEvent",
        (original_type,),
        {"__module__": original_type.__module__},
    )
    monkeypatch.setattr(support, "WeChatNativeEvent", reloaded_type)

    # Same class and message metadata, but not retained by the Native adapter.
    impersonator = original_type(event.message_obj, adapter)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        result = json.loads(await plugin.wechat_get_recent_messages(impersonator))
        assert result["status"] == "not_available"
        assert "reason=event_type_mismatch" in caplog.text
    finally:
        await adapter.terminate()


async def test_reinjected_event_keeps_adapter_ownership(serve, tmp_path, monkeypatch):
    adapter, event, msg = await tool_event(serve, tmp_path, enable_history_tools=True)
    original_type = type(event)
    reloaded_type = type(
        "WeChatNativeEvent",
        (original_type,),
        {"__module__": original_type.__module__},
    )
    monkeypatch.setattr(module("tool_support"), "WeChatNativeEvent", reloaded_type)

    async def messages(*args, **kwargs):
        return {"items": [msg], "nextCursor": None}

    adapter.client.messages = messages
    try:
        reinjected = adapter.create_event(event.message_obj)
        assert adapter._events[msg["id"]] is reinjected
        plugin = object.__new__(module("main").WeChatNativePlugin)
        accepted = json.loads(await plugin.wechat_get_recent_messages(reinjected))
        assert accepted["ok"] is True
        rejected = json.loads(await plugin.wechat_get_recent_messages(event))
        assert rejected["status"] == "not_available"
    finally:
        await adapter.terminate()


async def test_sticker_tool_replaces_retained_service_after_plugin_reload(serve, tmp_path):
    native, adapter, event = await send_event(serve, tmp_path)
    current_service = module("sticker_service").StickerService

    async def legacy_find(self, event, query="", limit=12):
        return {"ok": True, "status": "success", "action": "find", "items": [], "count": 0}

    previous_service = type(
        "StickerService",
        (current_service,),
        {"__module__": current_service.__module__, "find": legacy_find},
    )
    adapter.stickers = previous_service(adapter)
    try:
        result = await find(event)
        assert result["catalog"]["saved_count"] == 1
        assert result["catalog"]["added_pack_count"] == 1
        assert result["count"] == 2
        assert not any(c[0] == "POST" for c in native.calls)
    finally:
        await adapter.terminate()


async def test_replacing_retained_sticker_service_preserves_unknown_intent(serve, tmp_path):
    native, adapter, event = await save_event(serve, tmp_path)
    native.post_status = 503
    assert (await save(event))["status"] == "needs_review"
    current_service = module("sticker_service").StickerService

    async def legacy_save(self, event):
        return {"ok": False, "status": "failed", "action": "save"}

    previous_service = type(
        "StickerService",
        (current_service,),
        {"__module__": current_service.__module__, "save": legacy_save},
    )
    adapter.stickers = previous_service(adapter)
    try:
        assert (await save(event))["status"] == "needs_review"
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()


async def test_pack_send_uses_current_transport_with_retained_pre_upgrade_client(serve, tmp_path):
    from test_stickers import find, send_event, send_sticker

    native, adapter, event = await send_event(serve, tmp_path)
    native.pack_items[0]["original"] = None
    current = type(adapter.client)
    # Retain the actual HTTP client/session, with the class API from before the update.
    retained = type(
        "NativeClient",
        (),
        {
            name: member
            for name, member in vars(current).items()
            if name not in {"download_sticker", "__dict__", "__weakref__"}
        },
    )
    adapter.client.__class__ = retained
    assert not hasattr(adapter.client, "download_sticker")
    session = adapter.client._session
    try:
        selection = (await find(event, "晚安"))["items"][0]["selection_id"]
        outcome = await send_sticker(event, selection)
        assert outcome["status"] == "success", outcome
        assert adapter.client._session is session
        posts = [c for c in native.calls if c[0] == "POST"]
        assert [c[1].rsplit("/", 1)[1] for c in posts] == ["sticker-downloads", "sends"]
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("fault", ["http", "missing_receipt", "job_review", "foreign_job"])
async def test_acquisition_logs_distinguish_failure_boundary_without_reposting(
    serve, tmp_path, caplog, fault
):
    from test_stickers import send_sticker

    native, adapter, event = await send_event(serve, tmp_path)
    native.pack_items[0]["original"] = None
    if fault == "http":
        native.post_status = 503
        native.post_error_code = "media_queue_full"
    elif fault == "missing_receipt":
        row, _ = adapter.stickers.claim(
            native.raw,
            "acquire:pack1",
            "b" * 32,
            {"kind": "pack", "packId": "pack1", "stickerId": "b" * 32},
        )
        adapter.state.finish_sticker(row["idempotency_key"], "needs_review")
    else:
        original = native.job

        def job(kind="sticker_saved", job_id="job1"):
            value = original(kind, job_id)
            if kind == "sticker_acquire":
                value.update(
                    state="needs_review",
                    phase="submitting",
                    reason="sticker_submission_needs_review",
                )
                value["result"] = {"serverAcceptance": "pending", "private": "synthetic-secret"}
                if fault == "foreign_job":
                    value["accountId"] = "synthetic-secret"
                    value["kind"] = "synthetic-secret"
            return value

        native.job = job
    try:
        selection = (await find(event, "晚安"))["items"][0]["selection_id"]
        assert (await send_sticker(event, selection))[
            "reason"
        ] == "sticker_acquisition_needs_review"
        if fault == "http":
            assert "stage=submit http=503 code=media_queue_full" in caplog.text
        elif fault == "missing_receipt":
            assert "stage=missing_receipt" in caplog.text
        else:
            assert "native_state=needs_review native_phase=submitting" in caplog.text
            assert "native_code=sticker_submission_needs_review" in caplog.text
            assert f"identity_matches={fault != 'foreign_job'}" in caplog.text
        assert "synthetic-secret" not in caplog.text
        posts = [c for c in native.calls if c[0] == "POST"]
        await send_sticker(event, selection)
        await adapter.stickers.recover()
        assert len([c for c in native.calls if c[0] == "POST"]) == len(posts)
        assert not any(c[1].endswith("/sends") for c in posts)
    finally:
        await adapter.terminate()
