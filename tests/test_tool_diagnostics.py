import asyncio
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
        if fault == "missing_receipt":
            # Read-only startup recovery logs missing_receipt without posting.
            await adapter.stickers.recover()
            assert "stage=missing_receipt" in caplog.text
            assert not any(c[0] == "POST" for c in native.calls)
            # An explicit send reclaims the missing-receipt row with the original key.
            outcome = await send_sticker(event, selection)
            assert outcome["status"] == "success"
            posts = [c for c in native.calls if c[0] == "POST"]
            assert [c[1].rsplit("/", 1)[1] for c in posts] == ["sticker-downloads", "sends"]
            assert posts[0][3] == row["idempotency_key"]
            return
        assert (await send_sticker(event, selection))[
            "reason"
        ] == "sticker_acquisition_needs_review"
        if fault == "http":
            assert "stage=submit http=503 code=media_queue_full" in caplog.text
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


async def test_pre_http_acquisition_error_marks_failed_instead_of_needs_review(serve, tmp_path):
    from test_stickers import deliver, send_sticker

    native, adapter, event = await send_event(serve, tmp_path)
    native.pack_items[0]["original"] = None
    try:
        selection = (await find(event, "晚安"))["items"][0]["selection_id"]
        original_claim = adapter.stickers.claim
        close_once = True
        saved_session = adapter.client._session

        def claim_then_close(*args, **kwargs):
            nonlocal close_once
            row, won = original_claim(*args, **kwargs)
            if close_once and args[1].startswith("acquire:"):
                close_once = False
                adapter.client._session = None
            return row, won

        adapter.stickers.claim = claim_then_close
        outcome = await send_sticker(event, selection)
        assert outcome["status"] == "not_available"
        assert outcome["reason"] == "sticker_acquisition_failed"
        acquire_row = adapter.state.sticker_operation("a", "peer", "m1", "acquire:pack1")
        assert acquire_row["state"] == "failed"

        adapter.client._session = saved_session
        native.raw = dict(native.raw, id="m2", serverId="901")
        event2 = await deliver(adapter, native.raw)
        selection2 = (await find(event2, "晚安"))["items"][0]["selection_id"]
        assert (await send_sticker(event2, selection2))["status"] == "success"
    finally:
        await adapter.terminate()


async def test_missing_receipt_reclaim_keeps_concurrent_callers_single_post(serve, tmp_path):
    from test_stickers import deliver, send_sticker

    native, adapter, first_event = await send_event(serve, tmp_path)
    native.pack_items[0]["original"] = None
    row, _ = adapter.stickers.claim(
        native.raw,
        "acquire:pack1",
        "b" * 32,
        {"kind": "pack", "packId": "pack1", "stickerId": "b" * 32},
    )
    adapter.state.finish_sticker(row["idempotency_key"], "needs_review")
    entered = asyncio.Event()
    release = asyncio.Event()
    original_request = adapter.client._request
    messages = {"m1": dict(native.raw)}

    async def slow_request(method, path, **kwargs):
        if method == "POST" and path.endswith("/sticker-downloads"):
            entered.set()
            await release.wait()
        return await original_request(method, path, **kwargs)

    async def get_message(message_id):
        return messages[message_id]

    adapter.client._request = slow_request
    adapter.client.message = get_message
    try:
        raw2 = dict(native.raw, id="m2", serverId="901")
        messages["m2"] = raw2
        event2 = await deliver(adapter, raw2)
        raw3 = dict(native.raw, id="m3", serverId="902")
        messages["m3"] = raw3
        event3 = await deliver(adapter, raw3)
        sel2 = (await find(event2, "晚安"))["items"][0]["selection_id"]
        sel3 = (await find(event3, "晚安"))["items"][0]["selection_id"]

        task2 = asyncio.create_task(send_sticker(event2, sel2))
        await asyncio.wait_for(entered.wait(), 1)
        res3 = await send_sticker(event3, sel3)
        assert res3["reason"] == "sticker_acquisition_needs_review"
        release.set()
        res2 = await task2
        assert res2["status"] == "success"
        download_posts = [
            c for c in native.calls if c[0] == "POST" and c[1].endswith("/sticker-downloads")
        ]
        assert len(download_posts) == 1
        assert download_posts[0][3] == row["idempotency_key"]
    finally:
        await adapter.terminate()
