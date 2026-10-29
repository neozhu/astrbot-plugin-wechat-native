import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from conftest import module


def message(**updates):
    data = {
        "id": "m1",
        "accountId": "a",
        "threadId": "peer",
        "serverId": "900",
        "senderId": "user",
        "direction": "incoming",
        "type": 1,
        "body": "你好",
        "createdAt": datetime.now(UTC).isoformat(),
        "version": "1",
        "media": [],
        "mentions": {"state": "known", "self": False, "all": False, "userIds": []},
    }
    data.update(updates)
    return data


def native_event(msg, cursor="1", origin="live"):
    return {
        "cursor": cursor,
        "kind": "message.committed",
        "data": {"messageId": msg["id"], "threadId": msg["threadId"], "origin": origin},
    }


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    from astrbot.core.utils.metrics import Metric

    async def upload(**kwargs):
        pass

    monkeypatch.setattr(Metric, "upload", upload)


async def make_adapter(serve, tmp_path, msg=None, **options):
    msg = msg or message()
    sent = []

    async def handler(req):
        if "/messages/" in req.path:
            return web.json_response(msg)
        if req.method == "POST":
            sent.append((req.headers["Idempotency-Key"], await req.json()))
            return web.json_response(
                {
                    "id": "s1",
                    "state": "accepted",
                    "accountId": "a",
                    "destination": sent[-1][1]["threadId"],
                }
            )
        return web.json_response(
            {"id": "a", "ownUsername": "bot", "authenticated": True, "connection": "online"}
        )

    url = await serve(handler)
    cls = module("adapter").WeChatNativeAdapter
    queue = asyncio.Queue()
    cfg = {"id": "native-a", "server_url": url, "account_id": "a", "token": "fake", **options}
    adapter = cls(cfg, {}, queue, state_dir=tmp_path)
    await adapter.client.open()
    adapter.bot_self_id = "bot"
    return adapter, queue, msg, sent


async def test_private_event_real_framework_lifecycle_and_reply(serve, tmp_path):
    from astrbot.api.event import AstrMessageEvent, MessageChain
    from astrbot.api.message_components import Plain
    from astrbot.api.platform import MessageType

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        assert isinstance(event, AstrMessageEvent)
        assert event.message_obj.type == MessageType.FRIEND_MESSAGE
        assert event.message_str == "你好"
        assert event.get_sender_id() == "user"
        assert event.platform_meta.id == "native-a"
        assert event.message_obj.self_id == "bot"
        assert event.session.session_id == "a|private|peer"
        await event.send(MessageChain([Plain("回复")]))
        assert event._has_send_oper
        assert sent[0][1] == {"threadId": "peer", "text": "回复"}
        assert adapter.state.message_status("m1") == "replied"
        assert adapter.state.send_record(sent[0][0])["state"] == "accepted"
        await adapter.process_native_event(native_event(msg, "2"))
        assert queue.empty()
        assert adapter.state.cursor == "2"
    finally:
        await adapter.terminate()
    assert adapter.client.closed


@pytest.mark.parametrize(
    "updates,options,origin",
    [
        ({"direction": "outgoing"}, {}, "live"),
        ({"senderId": "bot"}, {}, "live"),
        ({"type": 3}, {}, "live"),
        ({"recall": {"observedAt": "now"}}, {}, "live"),
        ({}, {}, "history"),
        ({}, {}, "unknown"),
        ({"createdAt": (datetime.now(UTC) - timedelta(days=1)).isoformat()}, {}, "live"),
        ({}, {"enable_private_reply": False}, "live"),
        ({}, {"allowed_users": ["other"]}, "live"),
        ({"threadId": "room@chatroom"}, {}, "live"),
        ({"threadId": "room@chatroom", "mentions": {"state": "unknown", "self": True}}, {}, "live"),
        (
            {
                "threadId": "room@chatroom",
                "mentions": {"state": "known", "self": False, "all": True},
            },
            {},
            "live",
        ),
        ({"threadId": "room@chatroom"}, {"group_reply_mode": "disabled"}, "live"),
        (
            {"threadId": "room@chatroom"},
            {"group_reply_mode": "all", "allowed_groups": ["other"]},
            "live",
        ),
    ],
)
async def test_policy_drops_without_model_dispatch(serve, tmp_path, updates, options, origin):
    adapter, queue, msg, sent = await make_adapter(serve, tmp_path, message(**updates), **options)
    try:
        await adapter.process_native_event(native_event(msg, origin=origin))
        assert queue.empty()
        assert sent == []
        assert adapter.state.cursor == "1"
    finally:
        await adapter.terminate()


async def test_group_mentions_isolation_and_reply_target(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import At, Plain

    msg = message(threadId="room@chatroom", mentions={"state": "known", "self": True})
    adapter, queue, msg, sent = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        assert event.get_group_id() == "room@chatroom"
        assert event.session.session_id == "a|group|room%40chatroom|user"
        assert isinstance(event.message_obj.message[0], At)
        await event.send(MessageChain([At(qq="user"), Plain("answer")]))
        assert sent[0][1] == {
            "threadId": "room@chatroom",
            "text": "answer",
            "mentions": {"userIds": ["user"]},
        }
        msg.update(id="m2", senderId="other")
        await adapter.process_native_event(native_event(msg, "2"))
        assert queue.get_nowait().session.session_id != event.session.session_id
    finally:
        await adapter.terminate()


async def test_all_mode_shared_session(serve, tmp_path):
    msg = message(threadId="room@chatroom")
    adapter, queue, msg, _ = await make_adapter(
        serve, tmp_path, msg, group_reply_mode="all", unique_session=False
    )
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        assert event.session.session_id == "a|group|room%40chatroom"
        # Wake component lets AstrBot's normal waking stage accept explicit all mode.
        assert event.message_obj.message[0].qq == "bot"
    finally:
        await adapter.terminate()


async def test_recall_prevents_queued_reply(serve, tmp_path):
    from astrbot.api.event import MessageChain

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"messageId": "m1", "threadId": "peer", "serverId": "900"},
            }
        )
        await event.send(MessageChain().message("should not send"))
        assert event.is_stopped()
        assert not sent
        assert adapter.state.message_status("m1") == "recalled"
    finally:
        await adapter.terminate()


async def test_identity_mismatch_keeps_cursor_for_replay(serve, tmp_path):
    adapter, queue, msg, _ = await make_adapter(serve, tmp_path, message(accountId="foreign"))
    try:
        with pytest.raises(ValueError):
            await adapter.process_native_event(native_event(msg))
        assert adapter.state.cursor == "0"
        assert queue.empty()
    finally:
        await adapter.terminate()


async def test_run_reconnects_from_committed_cursor_and_stops(serve, tmp_path):
    connections = []
    resumed = asyncio.Event()
    release = asyncio.Event()
    msg = message()

    async def handler(req):
        if req.path == "/v1/status":
            return web.json_response({"serverId": "test-native-server"})
        if req.path.endswith("events/stream"):
            connections.append(req.query["after"])
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(req)
            if len(connections) == 1:
                await response.write(("data: " + json.dumps(native_event(msg)) + "\n\n").encode())
            else:
                resumed.set()
                await release.wait()
            return response
        if "/messages/" in req.path:
            return web.json_response(msg)
        return web.json_response(
            {"id": "a", "ownUsername": "bot", "authenticated": True, "connection": "online"}
        )

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"id": "a", "server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    adapter.reconnect_delay = 0.001
    task = asyncio.create_task(adapter.run())
    try:
        await asyncio.wait_for(resumed.wait(), 3)
        assert connections == ["0", "1"]
        assert adapter._event_queue.qsize() == 1
        await adapter.terminate()
        await asyncio.wait_for(task, 1)
        assert adapter.client.closed
    finally:
        release.set()
        await adapter.terminate()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_plugin_registers_config_in_real_astrbot():
    from astrbot.core.platform.register import platform_cls_map, platform_registry

    module("main")
    assert "wechat_native" in platform_cls_map
    metadata = next(x for x in platform_registry if x.name == "wechat_native")
    assert metadata.adapter_display_name == "WeChat Native"
    assert not metadata.support_streaming_message
    assert metadata.config_metadata["token_file"]["type"] == "string"
    assert metadata.default_config_tmpl["group_reply_mode"] == "mention"


async def test_startup_retries_and_preserves_framework_instance_identity(serve, tmp_path):
    attempts = 0
    connected = asyncio.Event()
    release = asyncio.Event()

    async def handler(req):
        nonlocal attempts
        if req.path == "/v1/status":
            return web.json_response({"serverId": "test-native-server"})
        if req.path.endswith("events/stream"):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(req)
            connected.set()
            await release.wait()
            return response
        attempts += 1
        if attempts == 1:
            return web.Response(status=503)
        return web.json_response(
            {"id": "a", "ownUsername": "bot", "authenticated": True, "connection": "online"}
        )

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    original_identity = adapter.client_self_id
    adapter.reconnect_delay = 0.001
    task = asyncio.create_task(adapter.run())
    try:
        await asyncio.wait_for(connected.wait(), 1)
        assert attempts == 2
        assert adapter.client_self_id == original_identity
        assert adapter.client_self_id != "a"
        assert adapter.bot_self_id == "bot"
    finally:
        release.set()
        await adapter.terminate()
        await asyncio.gather(task, return_exceptions=True)


async def test_concurrent_sends_stop_after_unknown_receipt(serve, tmp_path):
    from astrbot.api.event import MessageChain

    entered = asyncio.Event()
    release = asyncio.Event()
    posts = []
    msg = message()

    async def handler(req):
        if req.method == "POST":
            posts.append(await req.json())
            entered.set()
            await release.wait()
            return web.json_response(
                {"id": "s1", "state": "needs_review", "accountId": "a", "destination": "peer"}
            )
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    first = second = None
    try:
        await adapter.process_native_event(native_event(msg))
        event = adapter._event_queue.get_nowait()
        first = asyncio.create_task(event.send(MessageChain().message("first")))
        await entered.wait()
        second = asyncio.create_task(event.send(MessageChain().message("second")))
        await asyncio.sleep(0)  # both calls pass the initial gate before first receipt
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, Exception) for result in results)
        assert len(posts) == 1
        assert adapter.state.message_status("m1") == "needs_review"
    finally:
        release.set()
        await adapter.terminate()


async def test_recall_arriving_during_source_read_blocks_submission(serve, tmp_path):
    from astrbot.api.event import MessageChain

    entered = asyncio.Event()
    release = asyncio.Event()
    msg = message()
    reads = 0
    posts = []

    async def handler(req):
        nonlocal reads
        if req.method == "POST":
            posts.append(await req.json())
            return web.json_response(
                {"id": "s", "state": "accepted", "accountId": "a", "destination": "peer"}
            )
        reads += 1
        if reads == 2:
            entered.set()
            await release.wait()
        return web.json_response(msg)  # GET snapshot predates recall

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    try:
        await adapter.process_native_event(native_event(msg))
        event = adapter._event_queue.get_nowait()
        task = asyncio.create_task(event.send(MessageChain().message("late reply")))
        await entered.wait()
        await adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"messageId": "m1", "threadId": "peer", "serverId": "900"},
            }
        )
        release.set()
        await task
        assert posts == []
        assert event.is_stopped()
    finally:
        release.set()
        await adapter.terminate()


async def test_unsupported_output_failure_is_retained_without_send(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        with pytest.raises(ValueError):
            await event.send(MessageChain([Image.fromURL("https://invalid.example/image.png")]))
        assert not sent
        assert adapter.state.message_status("m1") == "failed"
    finally:
        await adapter.terminate()


def test_metadata_validates_with_astrbot_loader():
    from astrbot.core.star.star_manager import PluginManager
    from conftest import ROOT

    metadata = PluginManager._load_plugin_metadata(str(ROOT))
    assert metadata.name == "astrbot_plugin_wechat_native"
    assert metadata.display_name == "WeChat Native"
    assert metadata.author == "neozhu"


async def test_old_recall_identity_does_not_stop_current_event(serve, tmp_path):
    adapter, queue, msg, _ = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"messageId": "m1", "threadId": "peer", "serverId": "899"},
            }
        )
        assert not event.is_stopped()
        assert adapter.state.message_status("m1") == "queued"
    finally:
        await adapter.terminate()


async def test_stop_during_pending_send_retains_uncertainty(serve, tmp_path):
    from astrbot.api.event import MessageChain

    entered = asyncio.Event()
    release = asyncio.Event()
    msg = message()

    async def handler(req):
        if req.method == "POST":
            entered.set()
            await release.wait()
            return web.json_response(
                {"id": "s1", "state": "accepted", "accountId": "a", "destination": "peer"}
            )
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    await adapter.process_native_event(native_event(msg))
    event = adapter._event_queue.get_nowait()
    task = asyncio.create_task(event.send(MessageChain().message("pending")))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.gather(adapter.terminate(), adapter.terminate())
        assert task.done()
        assert adapter.client.closed
        path = await asyncio.to_thread(lambda: next(tmp_path.glob("*.sqlite3")))
        state = module("state").StateStore(path)
        assert state.message_status("m1") == "needs_review"
        assert state.db.execute("SELECT state FROM sends").fetchone()[0] == "needs_review"
        state.close()
    finally:
        release.set()
        await adapter.terminate()
        await asyncio.gather(task, return_exceptions=True)


async def test_send_by_persisted_session_and_foreign_account_gate(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.platform import MessageType
    from astrbot.core.platform.message_session import MessageSession

    adapter, _, _, sent = await make_adapter(serve, tmp_path)
    try:
        good = MessageSession("native-a", MessageType.FRIEND_MESSAGE, "a|private|peer")
        await adapter.send_by_session(good, MessageChain().message("proactive"))
        assert sent[0][1] == {"threadId": "peer", "text": "proactive"}
        bad = MessageSession("native-a", MessageType.FRIEND_MESSAGE, "other|private|peer")
        with pytest.raises(ValueError):
            await adapter.send_by_session(bad, MessageChain().message("bad"))
        assert len(sent) == 1
    finally:
        await adapter.terminate()


async def test_streaming_output_is_aggregated_once(serve, tmp_path):
    from astrbot.api.event import MessageChain

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()

        async def chunks():
            yield MessageChain().message("你")
            yield MessageChain().message("好")

        await event.send_streaming(chunks())
        assert [payload for _, payload in sent] == [{"threadId": "peer", "text": "你好"}]
    finally:
        await adapter.terminate()
