"""SPEC sticker scenarios through real adapter, HTTP transport and SQLite."""

import asyncio
import hashlib
import io
import json

import pytest
from aiohttp import web
from conftest import module
from PIL import Image as PILImage
from test_adapter import message, native_event


def gif():
    output = io.BytesIO()
    PILImage.new("RGB", (2, 2), "red").save(output, format="GIF")
    return output.getvalue()


GIF = gif()
CONTENT_ID = hashlib.md5(GIF).hexdigest()


def quoted(**changes):
    return message(
        type=49,
        appMessageType=57,
        body="请看看这个表情",
        quote={
            "type": 47,
            "state": "embedded",
            "embeddedSticker": True,
            "serverId": "800",
            "senderId": "other",
            **changes,
        },
    )


class Native:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []
        self.asset = {
            "id": "sticker-original",
            "accountId": "a",
            "messageId": raw["id"],
            "kind": "sticker",
            "variant": "original",
            "actualVariant": "original",
            "state": "available",
            "contentType": "image/gif",
            "byteLength": len(GIF),
            "sha256": hashlib.sha256(GIF).hexdigest(),
        }
        self.saved = []
        self.packs = []
        self.pack_items = []
        self.job_state = "available"
        self.acceptance = "accepted"
        self.writeback = "complete"
        self.post_status = 202
        self.last_kind = "sticker_saved"
        self.content_requests = []

    def job(self, kind="sticker_saved", job_id="job1"):
        return {
            "id": job_id,
            "accountId": "a",
            "kind": kind,
            "state": self.job_state,
            "phase": "completed" if self.job_state == "available" else "submitting",
            "threadId": self.raw["threadId"] if kind == "sticker_send" else None,
            "assetId": self.asset["id"] if kind == "download" else None,
            "serverMessageId": "999" if kind == "sticker_send" else None,
            "result": {
                "serverAcceptance": self.acceptance,
                "profileWriteback": self.writeback,
            },
        }

    async def handle(self, req):
        body = await req.json() if req.method == "POST" else None
        self.calls.append((req.method, req.path, body, req.headers.get("Idempotency-Key")))
        path = req.path
        if req.method == "POST":
            if self.post_status != 202:
                return web.json_response({}, status=self.post_status)
            kind = (
                "download"
                if path.endswith("media-transfers")
                else "sticker_send"
                if path.endswith("/sends")
                else "sticker_saved"
            )
            self.last_kind = kind
            if kind == "download":
                self.asset["state"] = "available"
            return web.json_response(self.job(kind), status=202)
        if "/jobs/" in path or path.endswith("by-idempotency-key"):
            return web.json_response(self.job(getattr(self, "last_kind", "sticker_saved")))
        if path.endswith("/media"):
            return web.json_response({"items": [self.asset]})
        if path.endswith("/content"):
            self.content_requests.append(dict(req.query))
            return web.Response(body=GIF, content_type="image/gif")
        if "/messages/" in path:
            return web.json_response(self.raw)
        if path.endswith("/custom-stickers"):
            return web.json_response(
                {"items": self.saved, "revision": "rev", "freshness": "profile"}
            )
        if "/custom-stickers/" in path:
            return web.json_response(
                next((s for s in self.saved if s["id"] == path.rsplit("/", 1)[1]), {})
            )
        if path.endswith("/sticker-packs"):
            assert req.query["membership"] == "added"
            return web.json_response({"items": self.packs, "coverage": "account_added"})
        if path.endswith("/stickers"):
            return web.json_response({"packId": "pack1", "items": self.pack_items})
        return web.json_response({}, status=404)


async def setup(serve, tmp_path, raw=None, **options):
    native = Native(raw or quoted())
    url = await serve(native.handle)
    adapter = module("adapter").WeChatNativeAdapter(
        {
            "id": "bot",
            "server_url": url,
            "account_id": "a",
            "token": "fake",
            "enable_stickers": True,
            **options,
        },
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    adapter.bot_self_id = "bot"
    await adapter.client.open()
    return native, adapter


async def deliver(adapter, raw):
    await adapter.process_native_event(native_event(raw, str(int(adapter.state.cursor) + 1)))
    if adapter._media_tasks:
        await asyncio.gather(*list(adapter._media_tasks))
    return adapter._event_queue.get_nowait()


@pytest.mark.parametrize("quote", [{}, {"type": 49, "appMessageType": 8}])
async def test_quoted_sticker_uses_reply_media_and_delivers_image(serve, tmp_path, quote):
    native, adapter = await setup(serve, tmp_path, quoted(**quote))
    try:
        event = await deliver(adapter, native.raw)
        from astrbot.api.message_components import Image, Reply

        assert any(isinstance(c, Image) for c in event.message_obj.message)
        assert any(
            isinstance(c, Reply) and "表情" in c.message_str for c in event.message_obj.message
        )
        assert "请看看这个表情" in event.message_str
        assert any(p.endswith("/messages/m1/media") for _, p, _, _ in native.calls)
        assert not any("800" in p for _, p, _, _ in native.calls)
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "change",
    [
        {"accountId": "other"},
        {"messageId": "other"},
        {"id": ""},
        {"state": "expired"},
        {"state": "unsupported"},
    ],
)
async def test_quote_rejects_invalid_asset_without_download(serve, tmp_path, change):
    native, adapter = await setup(serve, tmp_path)
    native.asset.update(change)
    try:
        with pytest.raises(module("native_client").MediaError):
            await adapter.stickers.read_quote(native.raw)
        assert not any(p.endswith("/content") for _, p, _, _ in native.calls)
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "change", [{"state": "recalled"}, {"state": "unsupported"}, {"embeddedSticker": False}]
)
async def test_invalid_quote_never_enters_visual_chain(serve, tmp_path, change):
    native, adapter = await setup(serve, tmp_path, quoted(**change))
    try:
        event = await deliver(adapter, native.raw)
        from astrbot.api.message_components import Image

        assert not any(isinstance(c, Image) for c in event.message_obj.message)
        assert not any(p.endswith("/media") for _, p, _, _ in native.calls)
    finally:
        await adapter.terminate()


async def save_event(serve, tmp_path, **options):
    raw = quoted()
    raw["body"] = "请收藏这个表情"
    native, adapter = await setup(serve, tmp_path, raw, wechat_action_admins=["user"], **options)
    return native, adapter, await deliver(adapter, raw)


async def save(event):
    plugin = object.__new__(module("main").WeChatNativePlugin)
    return json.loads(await plugin.wechat_save_quoted_sticker(event))


async def test_save_requires_confirmed_writeback_and_reuses_receipt(serve, tmp_path):
    native, adapter, event = await save_event(serve, tmp_path)
    try:
        native.writeback = "pending"
        assert (await save(event))["status"] == "needs_review"
        native.writeback = "complete"
        assert (await save(event))["status"] == "success"
        assert (await save(event))["status"] == "success"
        posts = [c for c in native.calls if c[0] == "POST"]
        assert len(posts) == 1
        assert posts[0][2] == {
            "source": {"kind": "message", "messageId": "m1", "assetId": "sticker-original"}
        }
        row = adapter.state.sticker_operation("a", "peer", "m1", "save")
        assert row["content_id"] == CONTENT_ID
        assert row["idempotency_key"] == posts[0][3]
        assert row["job_id"] == "job1" and row["state"] == "success"
    finally:
        await adapter.terminate()


async def test_already_saved_is_success_without_post_and_phone_deletion_is_observed(
    serve, tmp_path
):
    native, adapter, event = await save_event(serve, tmp_path)
    try:
        native.saved = [{"id": CONTENT_ID, "caption": None, "original": None}]
        data = await save(event)
        assert data["status"] == "already_saved" and data["ok"]
        assert not any(c[0] == "POST" for c in native.calls)
        native.raw = dict(native.raw, id="m2", serverId="901")
        native.asset["messageId"] = "m2"
        event2 = await deliver(adapter, native.raw)
        assert (await save(event2))["status"] == "already_saved"
        native.saved = []
        native.raw = dict(native.raw, id="m3", serverId="902")
        native.asset["messageId"] = "m3"
        assert (await save(await deliver(adapter, native.raw)))["status"] == "success"
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()


async def test_sqlite_claim_serializes_same_content_across_connections(tmp_path):
    State = module("state").StateStore
    first, second = State(tmp_path / "shared.sqlite3"), State(tmp_path / "shared.sqlite3")
    try:
        row1, won1 = first.claim_sticker("a", "room1", "m1", "save", CONTENT_ID, "k1", "h1", 1)
        row2, won2 = second.claim_sticker("a", "room2", "m2", "save", CONTENT_ID, "k2", "h2", 2)
        assert won1 and not won2
        assert row1["idempotency_key"] == row2["idempotency_key"] == "k1"
        first.finish_sticker("k1", "needs_review")
        assert not second.claim_sticker("a", "room3", "m3", "save", CONTENT_ID, "k3", "h3", 3)[1]
        first.finish_sticker("k1", "failed")
        assert second.claim_sticker("a", "room3", "m3", "save", CONTENT_ID, "k3", "h3", 3)[1]
    finally:
        first.close()
        second.close()


async def test_concurrent_save_and_lost_admission_never_resubmit_after_restart(serve, tmp_path):
    native, adapter, event = await save_event(serve, tmp_path)
    native.post_status = 503
    try:
        results = await asyncio.gather(*[save(event) for _ in range(4)])
        assert all(r["status"] == "needs_review" for r in results)
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
        cfg = dict(adapter.config)
        await adapter.terminate()
        adapter = module("adapter").WeChatNativeAdapter(
            cfg, {}, asyncio.Queue(), state_dir=tmp_path
        )
        adapter.bot_self_id = "bot"
        await adapter.client.open()
        await adapter.stickers.recover()
        native.raw = dict(native.raw, id="m2", serverId="901")
        native.asset["messageId"] = "m2"
        assert (await save(await deliver(adapter, native.raw)))["status"] == "needs_review"
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("change", ["admin", "intent", "original", "wxgf", "scope"])
async def test_save_denies_unauthorized_or_unverified_source(serve, tmp_path, change):
    native, adapter, event = await save_event(serve, tmp_path)
    try:
        if change == "admin":
            from dataclasses import replace

            adapter.native_config = replace(adapter.native_config, wechat_action_admins=frozenset())
        elif change == "intent":
            native.raw["body"] = event.message_obj.raw_message["body"] = "这个表情怎么样"
        elif change == "original":
            native.asset.update(variant="thumbnail", actualVariant="thumbnail")
        elif change == "wxgf":
            native.asset["contentType"] = "image/x-wxgf"
        else:
            native.raw = dict(native.raw, threadId="elsewhere")
        assert (await save(event))["status"] in {"permission_denied", "not_available"}
        assert not any(c[0] == "POST" for c in native.calls)
    finally:
        await adapter.terminate()


async def test_transfer_intent_is_durable_and_lost_response_is_never_reposted(serve, tmp_path):
    native, adapter = await setup(serve, tmp_path)
    native.asset["state"] = "pending"
    native.post_status = 503
    try:
        for _ in range(2):
            with pytest.raises(module("native_client").MediaError):
                await adapter.stickers.read_quote(native.raw)
        posts = [c for c in native.calls if c[0] == "POST"]
        assert len(posts) == 1 and posts[0][3]
        assert posts[0][2] == {
            "messageId": "m1",
            "assetId": "sticker-original",
            "quality": "original",
        }
    finally:
        await adapter.terminate()


async def find(event, query="", limit=12):
    plugin = object.__new__(module("main").WeChatNativePlugin)
    return json.loads(await plugin.wechat_find_stickers(event, query, limit))


async def send_sticker(event, selection):
    plugin = object.__new__(module("main").WeChatNativePlugin)
    return json.loads(await plugin.wechat_send_sticker(event, selection))


async def send_event(serve, tmp_path, **options):
    native, adapter = await setup(serve, tmp_path, message(), **options)
    native.saved = [{"id": CONTENT_ID, "caption": "开心", "original": None}]
    native.packs = [{"id": "pack1", "name": "小猫", "membership": "added"}]
    native.pack_items = [{"id": "b" * 32, "packId": "pack1", "caption": "晚安", "original": None}]
    return native, adapter, await deliver(adapter, native.raw)


async def test_catalog_is_bounded_metadata_only_and_selection_is_event_bound(serve, tmp_path):
    native, adapter, event = await send_event(serve, tmp_path)
    try:
        data = await find(event)
        assert data["ok"] and len(data["items"]) == 2
        assert {c["caption"] for c in data["items"]} == {"开心", "晚安"}
        assert not any(c[0] == "POST" for c in native.calls)
        assert len((await find(event, "晚安"))["items"]) == 1
        assert len((await find(event, "小猫"))["items"]) == 1
        assert not (await find(event, "未知含义"))["items"]
        assert (await find(event, limit=0))["status"] == "invalid_request"
        assert (await send_sticker(event, "made-up"))["status"] == "invalid_request"
        native.raw = dict(native.raw, id="m2", serverId="901")
        other = await deliver(adapter, native.raw)
        assert (await send_sticker(other, data["items"][0]["selection_id"]))[
            "status"
        ] == "invalid_request"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("source", ["saved", "pack"])
async def test_native_send_reuses_outbox_and_sends_one_selected_original(serve, tmp_path, source):
    native, adapter, event = await send_event(serve, tmp_path)
    try:
        choices = (await find(event))["items"]
        choice = next(c for c in choices if c["source"] == source)
        assert (await send_sticker(event, choice["selection_id"]))["status"] == "success"
        assert (await send_sticker(event, choices[1]["selection_id"]))["status"] == "success"
        posts = [c for c in native.calls if c[0] == "POST"]
        assert len(posts) == 1
        expected = {"kind": source, "stickerId": CONTENT_ID if source == "saved" else "b" * 32}
        if source == "pack":
            expected["packId"] = "pack1"
        assert posts[0][2] == {"kind": "sticker", "threadId": "peer", "source": expected}
        row = adapter.state.sticker_operation("a", "peer", "m1", "send")
        assert row["server_message_id"] == "999" and row["state"] == "success"
        assert adapter.state.send_record(row["idempotency_key"])["state"] == "accepted"
        assert adapter.state.message_status("m1") == "replied"
        assert not any("/jobs/" in c[1] for c in native.calls)
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("source", ["saved", "pack"])
async def test_membership_removed_after_find_prevents_post(serve, tmp_path, source):
    native, adapter, event = await send_event(serve, tmp_path)
    try:
        choice = next(c for c in (await find(event))["items"] if c["source"] == source)
        native.saved = []
        native.packs[0]["membership"] = "not_added"
        assert (await send_sticker(event, choice["selection_id"]))["status"] == "not_available"
        assert not any(c[0] == "POST" for c in native.calls)
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "fault", ["running", "unaccepted", "foreign", "no_server_id", "needs_review", "lost"]
)
async def test_unconfirmed_send_never_reports_success_or_submits_twice(serve, tmp_path, fault):
    native, adapter, event = await send_event(serve, tmp_path)
    adapter.client.send_timeout = 0.02
    adapter.client.poll_interval = 0.001
    old_job = native.job

    def job(kind="sticker_saved", job_id="job1"):
        data = old_job(kind, job_id)
        if fault == "running":
            data["state"] = "running"
        elif fault == "unaccepted":
            data["result"]["serverAcceptance"] = "pending"
        elif fault == "foreign":
            data["accountId"] = "other"
        elif fault == "no_server_id":
            data["serverMessageId"] = None
        elif fault == "needs_review":
            data["state"] = "needs_review"
        return data

    native.job = job
    if fault == "lost":
        native.post_status = 503
    try:
        selection = (await find(event))["items"][0]["selection_id"]
        assert (await send_sticker(event, selection))["status"] in {"pending", "needs_review"}
        assert (await send_sticker(event, selection))["status"] in {"pending", "needs_review"}
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()


async def test_group_send_interval_and_recall_guard(serve, tmp_path):
    native, adapter, event = await send_event(
        serve,
        tmp_path,
        group_reply_mode="all",
        group_reply_style="plain",
    )
    try:
        # Use a group with genuine mention metadata through the normal adapter.
        native.raw = message(
            id="m2",
            serverId="901",
            threadId="room@chatroom",
            mentions={"state": "known", "self": True},
        )
        event = await deliver(adapter, native.raw)
        selection = (await find(event))["items"][0]["selection_id"]
        assert (await send_sticker(event, selection))["status"] == "success"
        native.raw = dict(native.raw, id="m3", serverId="902")
        event2 = await deliver(adapter, native.raw)
        selection2 = (await find(event2))["items"][0]["selection_id"]
        assert (await send_sticker(event2, selection2))["status"] == "rate_limited"
        native.raw = dict(native.raw, id="m4", serverId="903", threadId="other@chatroom")
        event3 = await deliver(adapter, native.raw)
        selection3 = (await find(event3))["items"][0]["selection_id"]
        native.raw = dict(native.raw, recall={})
        assert (await send_sticker(event3, selection3))["status"] != "success"
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()


async def test_native_verified_preview_contract_is_preferred(serve, tmp_path):
    native, adapter = await setup(serve, tmp_path)
    # v2.4.0 MediaImagePreview has sourceSha256 and no state field.
    native.asset["preview"] = {
        "sourceSha256": native.asset["sha256"],
        "contentType": "image/gif",
        "byteLength": len(GIF),
        "sha256": native.asset["sha256"],
    }
    try:
        await adapter.stickers.read_quote(native.raw)
        assert native.content_requests == [{"representation": "preview"}]
        native.content_requests.clear()
        await adapter.stickers.read_quote(native.raw, original=True)
        assert native.content_requests == [{}]
    finally:
        await adapter.terminate()


async def test_asset_mutation_or_source_recall_during_content_read_is_rejected(serve, tmp_path):
    native, adapter = await setup(serve, tmp_path)
    original_content = adapter.client.media_content

    async def content(*args, **kwargs):
        data = await original_content(*args, **kwargs)
        native.asset["sha256"] = "0" * 64
        return data

    adapter.client.media_content = content
    try:
        with pytest.raises(module("native_client").MediaError):
            await adapter.stickers.read_quote(native.raw)
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("disable", ["feature", "media", "mention", "whitelist", "loop"])
async def test_sticker_switches_and_existing_access_policy(serve, tmp_path, disable):
    from dataclasses import replace

    native, adapter = await setup(serve, tmp_path)
    try:
        if disable in {"feature", "media"}:
            key = "enable_stickers" if disable == "feature" else "enable_media"
            adapter.native_config = replace(adapter.native_config, **{key: False})
            event = await deliver(adapter, native.raw)
            from astrbot.api.message_components import Image

            assert not any(isinstance(c, Image) for c in event.message_obj.message)
            assert not any(p.endswith("/media") for _, p, _, _ in native.calls)
            if disable == "feature":
                assert (await find(event))["status"] == "not_available"
        else:
            native.raw = dict(native.raw, threadId="room@chatroom")
            if disable == "whitelist":
                adapter.native_config = replace(
                    adapter.native_config, allowed_groups=frozenset({"elsewhere"})
                )
            elif disable == "loop":
                adapter.state.set_cooldown("a", "room@chatroom", 10**12)
            await adapter.process_native_event(native_event(native.raw))
            assert adapter._event_queue.empty() and not adapter._media_tasks
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("action", ["save", "send"])
async def test_receipts_recover_after_restart_using_reads_only(serve, tmp_path, action):
    if action == "save":
        native, adapter, event = await save_event(serve, tmp_path)
        native.job_state = "running"
        assert (await save(event))["status"] == "pending"
    else:
        native, adapter, event = await send_event(serve, tmp_path)
        native.job_state = "running"
        adapter.client.send_timeout = 0.01
        assert (await send_sticker(event, (await find(event))["items"][0]["selection_id"]))[
            "status"
        ] == "needs_review"
    cfg = dict(adapter.config)
    await adapter.terminate()
    adapter = module("adapter").WeChatNativeAdapter(cfg, {}, asyncio.Queue(), state_dir=tmp_path)
    adapter.bot_self_id = "bot"
    await adapter.client.open()
    native.job_state = "available"
    try:
        await adapter.stickers.recover()
        row = adapter.state.sticker_operation("a", "peer", "m1", action)
        assert row["state"] == "success"
        if action == "send":
            assert adapter.state.message_status("m1") == "replied"
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()


async def test_successful_transfer_persists_job_and_never_posts_again(serve, tmp_path):
    native, adapter = await setup(serve, tmp_path)
    native.asset["state"] = "pending"
    try:
        assert (await adapter.stickers.read_quote(native.raw))[0] == GIF
        row = adapter.state.sticker_operation("a", "peer", "m1", "transfer:sticker-original")
        assert row["job_id"] == "job1" and row["state"] == "success"
        await adapter.stickers.read_quote(native.raw)
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()


async def test_parallel_native_send_calls_share_one_intent(serve, tmp_path):
    native, adapter, event = await send_event(serve, tmp_path)
    try:
        selections = (await find(event))["items"]
        replies = await asyncio.gather(
            *[send_sticker(event, c["selection_id"]) for c in selections]
        )
        assert all(r["status"] in {"success", "needs_review"} for r in replies)
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()


async def test_adapter_startup_recovers_sticker_jobs_without_new_post(serve, tmp_path):
    native, adapter = await setup(serve, tmp_path)
    row, _ = adapter.stickers.claim(native.raw, "save", CONTENT_ID, {"assetId": "sticker-original"})
    adapter.state.finish_sticker(row["idempotency_key"], "pending", "job1")

    async def account():
        return {"id": "a", "ownUsername": "bot", "authenticated": True, "connection": "online"}

    async def status():
        return {"serverId": "native-test"}

    async def events(cursor):
        for _ in range(100):
            if adapter.state.sticker_operation("a", "peer", "m1", "save")["state"] == "success":
                break
            await asyncio.sleep(0.001)
        adapter.stopped = True
        if False:
            yield {}

    adapter.client.account, adapter.client.status, adapter.client.events = account, status, events
    try:
        await adapter.run()
        assert adapter.state.sticker_operation("a", "peer", "m1", "save")["state"] == "success"
        assert not any(c[0] == "POST" for c in native.calls)
    finally:
        await adapter.terminate()


async def test_save_reconciled_noop_is_success_and_bad_job_identity_stays_blocked(serve, tmp_path):
    native, adapter, event = await save_event(serve, tmp_path)
    native.acceptance = "reconciled_noop"
    try:
        assert (await save(event))["status"] == "success"
        native.raw = dict(native.raw, id="m2", serverId="901")
        native.asset["messageId"] = "m2"
        event2 = await deliver(adapter, native.raw)
        native.job_state = "running"
        assert (await save(event2))["status"] == "pending"
        original_job = native.job
        native.job = lambda *a, **kw: dict(original_job(*a, **kw), accountId="other")
        assert (await save(event2))["status"] == "needs_review"
        assert len([c for c in native.calls if c[0] == "POST"]) == 2
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "body", ["收藏功能怎么用", "这个表情值得收藏", "你收藏过这个吗", "不要收藏这个表情"]
)
async def test_descriptive_or_negative_save_text_is_not_authorization(serve, tmp_path, body):
    native, adapter, event = await save_event(serve, tmp_path)
    native.raw["body"] = event.message_obj.raw_message["body"] = body
    try:
        assert (await save(event))["status"] == "permission_denied"
        assert not any(c[0] == "POST" for c in native.calls)
    finally:
        await adapter.terminate()


def test_accepted_sticker_receipt_settles_outbox_and_message_in_one_transaction(tmp_path):
    State = module("state").StateStore
    path = tmp_path / "atomic.sqlite3"
    state = State(path)
    state.accept("1", "m1", "peer", "900", "queued")
    state.claim_sticker("a", "peer", "m1", "send", CONTENT_ID, "key", "hash", 1)
    state.begin_send("key", "m1", "peer")
    try:
        state.complete_sticker_send("key", "job1", "999")
    finally:
        state.close()
    state = State(path)
    try:
        assert state.sticker_operation("a", "peer", "m1", "send")["state"] == "success"
        assert state.send_record("key")["state"] == "accepted"
        assert state.message_status("m1") == "replied"
        # A delayed unsuccessful read from a concurrent caller cannot undo the receipt.
        state.finish_sticker("key", "needs_review")
        assert state.sticker_operation("a", "peer", "m1", "send")["state"] == "success"
    finally:
        state.close()


async def test_delayed_send_acceptance_still_counts_toward_loop_limit(serve, tmp_path):
    native, adapter, event = await send_event(serve, tmp_path)
    native.job_state = "running"
    adapter.client.send_timeout = 0.01
    try:
        selection = (await find(event))["items"][0]["selection_id"]
        assert (await send_sticker(event, selection))["status"] == "needs_review"
        for i in range(7):
            adapter.loop_guard.outgoing("peer", f"previous-{i}", "")
        assert not adapter.loop_guard.blocked("peer")
        native.job_state = "available"
        adapter.client.send_timeout = 1
        await adapter.stickers.recover()
        assert adapter.loop_guard.blocked("peer")
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("policy", ["admin", "media", "feature"])
async def test_save_rechecks_permission_after_last_catalog_await(serve, tmp_path, policy):
    from dataclasses import replace

    native, adapter, event = await save_event(serve, tmp_path)
    catalog = adapter.client.custom_stickers

    async def revoked():
        data = await catalog()
        change = (
            {"wechat_action_admins": frozenset()}
            if policy == "admin"
            else {"enable_media" if policy == "media" else "enable_stickers": False}
        )
        adapter.native_config = replace(adapter.native_config, **change)
        return data

    adapter.client.custom_stickers = revoked
    try:
        assert (await save(event))["status"] in {"permission_denied", "not_available"}
        assert not any(c[0] == "POST" for c in native.calls)
    finally:
        await adapter.terminate()


def test_invalid_sticker_frame_reports_unsupported_format():
    with pytest.raises(module("native_client").MediaError, match="unsupported_format"):
        module("event").sticker_preview(b"GIF89a\x02\x00\x02\x00\x00\x00\x00;")


async def test_existing_native_media_transfer_is_polled_without_post(serve, tmp_path):
    native, adapter = await setup(serve, tmp_path)
    native.asset.update(state="pending", jobId="job1")
    native.last_kind = "download"
    original_job = native.job

    def job(*args, **kwargs):
        native.asset["state"] = "available"
        return original_job(*args, **kwargs)

    native.job = job
    try:
        assert (await adapter.stickers.read_quote(native.raw))[0] == GIF
        assert not any(c[0] == "POST" for c in native.calls)
    finally:
        await adapter.terminate()


async def test_failed_save_without_remote_rejection_evidence_keeps_content_blocked(serve, tmp_path):
    native, adapter, event = await save_event(serve, tmp_path)
    native.job_state = "failed"
    original_job = native.job
    native.job = lambda *args, **kwargs: dict(original_job(*args, **kwargs), result={})
    try:
        assert (await save(event))["status"] == "needs_review"
        native.raw = dict(native.raw, id="m2", serverId="901")
        native.asset["messageId"] = "m2"
        assert (await save(await deliver(adapter, native.raw)))["status"] == "needs_review"
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()
