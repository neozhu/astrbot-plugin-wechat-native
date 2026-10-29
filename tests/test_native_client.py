import asyncio
import json

import pytest
from aiohttp import web
from conftest import module


def client(url, **kwargs):
    return module("native_client").NativeClient(url + "/v1", "account/a", "fake-token", **kwargs)


def receipt(state="accepted", **kwargs):
    return {
        "id": "s1",
        "accountId": "account/a",
        "destination": "room@chatroom",
        "state": state,
        "serverId": "9007199254740999",
        "errorCode": None,
        **kwargs,
    }


async def test_authenticated_encoded_requests_and_polling(serve):
    seen = []

    async def handler(req):
        assert req.headers["Authorization"] == "Bearer fake-token"
        seen.append((req.method, req.raw_path))
        if req.method == "POST":
            assert req.headers["Idempotency-Key"] == "stable-key"
            assert await req.json() == {
                "threadId": "room@chatroom",
                "text": "hello",
                "mentions": {"userIds": ["u"]},
            }
            return web.json_response(receipt("queued"), status=202)
        if "/sends/" in req.path:
            return web.json_response(receipt())
        return web.json_response({"id": "m/1"})

    url = await serve(handler)
    async with client(url, poll_interval=0.001) as c:
        await c.accounts()
        await c.account()
        await c.message("m/1")
        result = await c.send_text("room@chatroom", "hello", "stable-key", {"userIds": ["u"]})
    assert result["state"] == "accepted"
    assert ("GET", "/v1/accounts/account%2Fa/messages/m%2F1") in seen
    assert sum(method == "POST" for method, _ in seen) == 1
    assert c.closed


@pytest.mark.parametrize("state", ["needs_review", "failed", "cancelled", "unknown"])
async def test_terminal_send_errors_are_never_reposted(serve, state):
    requests = []

    async def handler(req):
        requests.append(req.method)
        return web.json_response(receipt(state))

    url = await serve(handler)
    api = module("native_client")
    async with client(url) as c:
        with pytest.raises(api.SendError) as error:
            await c.send_text("room@chatroom", "hello", "k")
    assert error.value.state == state
    assert requests == ["POST"]


async def test_submission_uncertainty_recovers_only_by_original_key(serve):
    requests = []

    async def handler(req):
        requests.append((req.method, req.path))
        if req.method == "POST":
            return web.json_response({"error": {"code": "timeout"}}, status=503)
        assert req.query["key"] == "original-key"
        return web.json_response(receipt())

    url = await serve(handler)
    async with client(url) as c:
        assert (await c.send_text("room@chatroom", "hi", "original-key"))["id"] == "s1"
    assert requests == [
        ("POST", "/v1/accounts/account/a/sends"),
        ("GET", "/v1/accounts/account/a/sends/by-idempotency-key"),
    ]


async def test_cross_thread_receipt_rejected(serve):
    async def handler(req):
        return web.json_response(receipt(destination="foreign"))

    url = await serve(handler)
    api = module("native_client")
    async with client(url) as c:
        with pytest.raises(api.SendError, match="receipt_identity_mismatch"):
            await c.send_text("room@chatroom", "hi", "k")


async def test_sse_chunking_heartbeat_unicode_and_resume(serve):
    async def handler(req):
        assert req.query["after"] == "9007199254740999"
        assert req.headers["Last-Event-ID"] == "9007199254740999"
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(req)
        data = {
            "cursor": "9007199254741000",
            "kind": "message.committed",
            "data": {"messageId": "消息", "threadId": "p", "origin": "live"},
        }
        raw = (
            ": heartbeat\r\n\r\nid: 9007199254741000\r\ndata: "
            + json.dumps(data, ensure_ascii=False)
            + "\r\n\r\n"
        ).encode()
        for i in range(0, len(raw), 3):
            await response.write(raw[i : i + 3])
        return response

    url = await serve(handler)
    async with client(url) as c:
        events = [event async for event in c.events("9007199254740999")]
    assert len(events) == 1
    assert events[0]["data"]["messageId"] == "消息"
    assert events[0]["cursor"] == "9007199254741000"


async def test_sse_cursor_expired_error_is_actionable(serve):
    async def handler(req):
        return web.Response(
            text='event: error\ndata: {"error":{"code":"event_cursor_expired"}}\n\n',
            content_type="text/event-stream",
        )

    url = await serve(handler)
    api = module("native_client")
    async with client(url) as c:
        with pytest.raises(api.NativeAPIError) as error:
            async for _ in c.events("0"):
                pass
    assert error.value.status == 410
    assert error.value.code == "event_cursor_expired"


async def test_no_redirect_token_leak_or_error_body_in_exception(serve):
    hits = []

    async def target(req):
        hits.append(req.path)
        return web.json_response({})

    target_url = await serve(target)

    async def handler(req):
        return web.Response(
            status=302, headers={"Location": target_url + "/secret"}, text="fake-token PRIVATE CHAT"
        )

    url = await serve(handler)
    api = module("native_client")
    async with client(url) as c:
        with pytest.raises(api.NativeAPIError) as error:
            await c.accounts()
    assert hits == []
    assert "fake-token" not in str(error.value)
    assert "PRIVATE CHAT" not in str(error.value)


async def test_send_poll_deadline(serve):
    async def handler(req):
        return web.json_response(receipt("submitting"))

    url = await serve(handler)
    api = module("native_client")
    async with client(url, send_timeout=0.01, poll_interval=0.001) as c:
        with pytest.raises(api.SendError) as error:
            await asyncio.wait_for(c.send_text("room@chatroom", "hi", "k"), 1)
    assert error.value.state == "submitting"
