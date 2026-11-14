"""Sticker resources tied to the current trusted message and Native account."""

import asyncio
import hashlib
import json
import re
import time
import uuid

from .native_client import MediaError, NativeAPIError, SendError
from .tool_support import ToolError, context, result


def source_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def is_sticker(raw):
    return isinstance(raw, dict) and (
        raw.get("type") == 47 or (raw.get("type") == 49 and raw.get("appMessageType") == 8)
    )


def quoted_sticker(raw):
    return (
        raw.get("type") == 49
        and raw.get("appMessageType") == 57
        and is_sticker(raw.get("quote"))
        and raw["quote"].get("state") == "embedded"
        and raw["quote"].get("embeddedSticker") is True
    )


def explicit_save_request(text):
    """Require an imperative about this quote, not a mention of the feature."""
    if not isinstance(text, str) or re.search(r"不要|不想|别|不许|do\s+not|don't", text, re.I):
        return False
    text = re.sub(r"^(?:@[^\u2005\r\n]+\u2005[ \t]*)+", "", text)
    pattern = (
        r"(?:(?:请|帮我|替我|给我|麻烦你?)\s*)*"
        r"(?:(?:把|将)(?:这个|这张|该|引用的)?表情(?:包)?)?"
        r"(?:收藏|保存)(?:(?:这个|这张|该|引用的)?表情(?:包)?|这个)?"
        r"(?:一下|起来|到(?:微信)?收藏(?:里|中)?|吧|谢谢)*"
    )
    return any(
        re.fullmatch(pattern, clause.strip())
        or re.fullmatch(r"(?:please\s+)?save\s+(?:this|the quoted)\s+sticker", clause.strip(), re.I)
        for clause in re.split(r"[，,。.!！\n]", text)
    )


class StickerService:
    def __init__(self, adapter):
        self.adapter = adapter

    async def check_quote(self, raw):
        adapter = self.adapter
        if (
            raw.get("accountId") != adapter.native_config.account_id
            or not quoted_sticker(raw)
            or raw.get("recall") is not None
            or adapter.state.message_status(raw["id"]) == "recalled"
            or adapter.state.is_recalled(raw["threadId"], raw["quote"].get("serverId"))
        ):
            raise MediaError(
                "recalled" if raw.get("quote", {}).get("state") == "recalled" else "unavailable"
            )
        current = await adapter.client.message(raw["id"])
        if (
            any(
                current.get(k) != raw.get(k)
                for k in ("id", "accountId", "threadId", "serverId", "senderId")
            )
            or current.get("quote") != raw.get("quote")
            or not quoted_sticker(current)
        ):
            raise MediaError("unavailable")
        if current.get("recall") is not None:
            raise MediaError("recalled")

    async def read_quote(self, raw, *, original=False, guard=None):
        try:
            async with asyncio.timeout(self.adapter.client.media_timeout):
                if guard:
                    guard()
                await self.check_quote(raw)
                assets = await self.adapter.client.sticker_assets(raw["id"])
                candidates = [a for a in assets if not original or a.get("variant") == "original"]
                if not candidates:
                    raise MediaError("unsupported_format")
                asset = sorted(candidates, key=lambda a: a.get("variant") != "original")[0]
                if asset.get("state") == "pending":
                    await self.transfer(raw, asset)
                    assets = await self.adapter.client.sticker_assets(raw["id"])
                    asset = next((a for a in assets if a["id"] == asset["id"]), None)
                    if asset is None:
                        raise MediaError("unavailable")
                data = await self.adapter.client.sticker_content(asset, original=original)
                refreshed = await self.adapter.client.sticker_assets(raw["id"])
                latest = next((a for a in refreshed if a["id"] == asset["id"]), None)
                if not latest or any(
                    latest.get(k) != asset.get(k)
                    for k in (
                        "kind",
                        "variant",
                        "actualVariant",
                        "state",
                        "sha256",
                        "byteLength",
                        "preview",
                    )
                ):
                    raise MediaError("unavailable")
                await self.check_quote(raw)
                if guard:
                    guard()
                return data, asset
        except NativeAPIError as error:
            from .native_client import _map_media_code

            raise _map_media_code(error.code, quoted=True, status=error.status) from None
        except TimeoutError:
            raise MediaError("unavailable") from None

    def claim(self, raw, action, content_id, source):
        digest = source_hash(source)
        key = "astrbot-sticker-" + source_hash(
            [
                self.adapter.native_config.api_url,
                raw["accountId"],
                raw["threadId"],
                raw["id"],
                action,
                digest,
            ]
        )
        return self.adapter.state.claim_sticker(
            raw["accountId"],
            raw["threadId"],
            raw["id"],
            action,
            content_id,
            key,
            digest,
            time.time(),
        )

    async def inspect(self, row, job=None):
        action, key = row["action"], row["idempotency_key"]
        if action == "send":
            return await self.inspect_send(row)
        if row["state"] in {"success", "already_saved", "failed"}:
            return result(action, row["state"])
        state = "needs_review"
        job_id = row["job_id"]
        server_id = None
        try:
            if job is None:
                if not job_id:
                    return result(action, state)
                job = await self.adapter.client.job(job_id)
            expected_kind = "sticker_saved" if action == "save" else "download"
            if (
                not isinstance(job, dict)
                or job.get("accountId") != row["account"]
                or job.get("kind") != expected_kind
                or not isinstance(job.get("id"), str)
                or not job["id"]
                or (job_id and job["id"] != job_id)
                or (action.startswith("transfer:") and job.get("assetId") != row["content_id"])
            ):
                raise ToolError("needs_review")
            job_id = job["id"]
            payload = job.get("result") or {}
            if job.get("state") == "available" and job.get("phase") == "completed":
                if action.startswith("transfer:") or (
                    payload.get("profileWriteback") == "complete"
                    and payload.get("serverAcceptance") in {"accepted", "reconciled_noop"}
                ):
                    state = "success"
            elif job.get("state") in {"queued", "running"}:
                state = "pending"
            elif job.get("state") in {"failed", "cancelled", "expired", "unsupported"}:
                if action.startswith("transfer:") or payload.get("serverAcceptance") in {
                    "not_attempted",
                    "rejected",
                }:
                    state = "failed"
        except (NativeAPIError, ToolError, TypeError, AttributeError):
            state = "needs_review"
        self.adapter.state.finish_sticker(key, state, job_id, server_id)
        return result(action, state)

    async def transfer(self, raw, asset):
        row, won = self.claim(
            raw,
            "transfer:" + asset["id"],
            asset["id"],
            {
                "messageId": raw["id"],
                "assetId": asset["id"],
                "quality": "original",
            },
        )
        try:
            active_job = asset.get("jobId")
            if won and isinstance(active_job, str) and active_job:
                self.adapter.state.finish_sticker(row["idempotency_key"], "pending", active_job)
                row = {**row, "job_id": active_job}
                outcome = await self.inspect(row)
            elif won:
                job = await self.adapter.client.transfer_media(
                    raw["id"],
                    asset_id=asset["id"],
                    key=row["idempotency_key"],
                )
                outcome = await self.inspect(row, job)
            else:
                outcome = await self.inspect(row)
            while outcome["status"] == "pending":
                await asyncio.sleep(self.adapter.client.poll_interval)
                row = self.adapter.state.sticker_operation(
                    row["account"],
                    row["thread"],
                    row["trigger_message_id"],
                    row["action"],
                )
                outcome = await self.inspect(row)
            if outcome["status"] != "success":
                raise MediaError("unavailable")
        except BaseException:
            if won:
                self.adapter.state.finish_sticker(row["idempotency_key"], "needs_review")
            raise

    async def save(self, event):
        def authorize():
            ctx = context(event, stickers=True)
            if (
                ctx.sender not in self.adapter.native_config.wechat_action_admins
                or not explicit_save_request(event.message_obj.raw_message.get("body", ""))
            ):
                raise ToolError("permission_denied")
            if not self.adapter.native_config.enable_media:
                raise ToolError("not_available")
            return ctx

        ctx = authorize()
        raw = event.message_obj.raw_message
        existing = self.adapter.state.sticker_operation(
            ctx.account, ctx.thread, ctx.message_id, "save"
        )
        if existing:
            return await self.inspect(existing)
        try:
            data, asset = await self.read_quote(raw, original=True)
            content_id = hashlib.md5(data).hexdigest()
            saved = await self.adapter.client.custom_stickers()
            if not isinstance(saved.get("items"), list):
                raise ToolError("not_available")
            await self.check_quote(raw)
            authorize()
        except MediaError:
            raise ToolError("not_available", "sticker_original_unavailable") from None
        row, won = self.claim(
            raw,
            "save",
            content_id,
            {
                "messageId": raw["id"],
                "assetId": asset["id"],
                "contentId": content_id,
            },
        )
        if not won:
            return await self.inspect(row)
        key = row["idempotency_key"]
        if any(
            isinstance(s, dict) and str(s.get("id", "")).lower() == content_id
            for s in saved["items"]
        ):
            self.adapter.state.finish_sticker(key, "already_saved")
            return result("save", "already_saved")
        try:
            job = await self.adapter.client.save_sticker(raw["id"], asset["id"], key)
            return await self.inspect(row, job)
        except BaseException as error:
            state = (
                "failed"
                if isinstance(error, NativeAPIError)
                and 400 <= error.status < 500
                and error.status not in {408, 409, 429}
                else "needs_review"
            )
            self.adapter.state.finish_sticker(key, state)
            if isinstance(error, asyncio.CancelledError):
                raise
            return result("save", state)

    async def recover(self):
        """Only query retained jobs. Missing admission receipts never authorize POST."""
        for row in self.adapter.state.pending_stickers(self.adapter.native_config.account_id):
            await self.inspect(row)

    async def catalog(self):
        saved = await self.adapter.client.custom_stickers()
        packs = await self.adapter.client.sticker_packs()
        if not isinstance(saved.get("items"), list) or not isinstance(packs.get("items"), list):
            raise ToolError("not_available")
        items = []
        for item in saved["items"]:
            if isinstance(item, dict) and re.fullmatch(r"[0-9a-fA-F]{32}", str(item.get("id", ""))):
                items.append(
                    {
                        "source": {"kind": "saved", "stickerId": item["id"]},
                        "caption": item.get("caption") or "",
                        "pack_name": "",
                    }
                )
        # Bound metadata work; no downloads or automatic full-pack acquisition.
        for pack in packs["items"][:50]:
            if not isinstance(pack, dict) or pack.get("membership") != "added":
                continue
            pack_id = pack.get("id")
            if not isinstance(pack_id, str) or not pack_id:
                continue
            page = await self.adapter.client.pack_stickers(pack_id)
            if page.get("packId") != pack_id or not isinstance(page.get("items"), list):
                raise ToolError("not_available")
            for item in page["items"]:
                if (
                    not isinstance(item, dict)
                    or item.get("packId") != pack_id
                    or not re.fullmatch(r"[0-9a-fA-F]{32}", str(item.get("id", "")))
                ):
                    raise ToolError("not_available")
                name = pack.get("name")
                items.append(
                    {
                        "source": {"kind": "pack", "packId": pack_id, "stickerId": item["id"]},
                        "caption": item.get("caption") or "",
                        "pack_name": name if isinstance(name, str) and name != pack_id else "",
                    }
                )
        return items

    async def find(self, event, query="", limit=12):
        ctx = context(event, stickers=True)
        if (
            not isinstance(query, str)
            or len(query.encode()) > 256
            or type(limit) is not int
            or not 1 <= limit <= 12
        ):
            raise ToolError("invalid_request")
        catalog = await self.catalog()
        context(event, stickers=True)
        retained = event.get_extra("wechat_native_sticker_selections") or {}
        choices = []
        for item in catalog:
            caption, name = str(item["caption"])[:160], item["pack_name"][:160]
            if query and query.casefold() not in (caption + " " + name).casefold():
                continue
            token = uuid.uuid4().hex
            retained[token] = {
                "account": ctx.account,
                "thread": ctx.thread,
                "trigger": ctx.message_id,
                "source": item["source"],
                "created": time.time(),
            }
            choices.append(
                {
                    "selection_id": token,
                    "source": item["source"]["kind"],
                    "caption": caption,
                    "pack_name": name,
                }
            )
            if len(choices) == limit:
                break
        # Prevent repeated searches from growing one event indefinitely.
        event.set_extra("wechat_native_sticker_selections", dict(list(retained.items())[-120:]))
        return result("find", "success", items=choices, count=len(choices), untrusted_data=True)

    async def validate_selection(self, source):
        if source["kind"] == "saved":
            page = await self.adapter.client.custom_stickers()
            valid = any(
                isinstance(i, dict) and i.get("id") == source["stickerId"]
                for i in page.get("items", [])
            )
        else:
            packs = await self.adapter.client.sticker_packs()
            valid = any(
                isinstance(p, dict)
                and p.get("id") == source["packId"]
                and p.get("membership") == "added"
                for p in packs.get("items", [])
            )
            if valid:
                page = await self.adapter.client.pack_stickers(source["packId"])
                valid = page.get("packId") == source["packId"] and any(
                    isinstance(i, dict)
                    and i.get("id") == source["stickerId"]
                    and i.get("packId") == source["packId"]
                    for i in page.get("items", [])
                )
        if not valid:
            raise ToolError("not_available", "sticker_membership_changed")

    async def inspect_send(self, row):
        key = row["idempotency_key"]
        if row["state"] in {"success", "failed"}:
            return result("send", row["state"])
        try:
            receipt = await self.adapter.client.sticker_send_receipt(
                row["thread"],
                key,
                job_id=row["job_id"],
                record_job=lambda job_id: self.adapter.state.finish_sticker(key, "pending", job_id),
            )
        except SendError as error:
            state = "failed" if error.state == "failed" else "needs_review"
            self.adapter.state.finish_sticker(key, state, error.send_id)
            return result("send", state)
        self.adapter.state.complete_sticker_send(key, receipt["id"], receipt["serverMessageId"])
        self.adapter.loop_guard.outgoing(row["thread"], key, "", trigger=row["trigger_message_id"])
        return result("send", "success")

    async def send(self, event, selection_id):
        ctx = context(event, stickers=True, sticker_recovery=True)
        selections = event.get_extra("wechat_native_sticker_selections") or {}
        selection = selections.get(selection_id) if isinstance(selection_id, str) else None
        if (
            not selection
            or (selection["account"], selection["thread"], selection["trigger"])
            != (ctx.account, ctx.thread, ctx.message_id)
            or time.time() - selection["created"] > 300
        ):
            raise ToolError("invalid_request")
        existing = self.adapter.state.sticker_operation(
            ctx.account, ctx.thread, ctx.message_id, "send"
        )
        if existing:
            return await self.inspect_send(existing)
        source = selection["source"]
        await self.validate_selection(source)
        context(event, stickers=True)
        raw = event.message_obj.raw_message
        row, won = self.claim(raw, "send", source["stickerId"], source)
        if row is None:
            raise ToolError("rate_limited")
        if not won:
            return await self.inspect_send(row)
        key = row["idempotency_key"]
        try:
            sent = await self.adapter.send_chain(
                ctx.thread,
                None,
                source=raw,
                event=event,
                sticker=source,
                sticker_key=key,
            )
            if not sent:
                self.adapter.state.finish_sticker(key, "failed")
                return result("send", "failed")
        except BaseException as error:
            state = (
                "failed"
                if isinstance(error, (ValueError, SendError))
                and (not isinstance(error, SendError) or error.state == "failed")
                else "needs_review"
            )
            self.adapter.state.finish_sticker(key, state)
            if isinstance(error, asyncio.CancelledError):
                raise
            return result("send", state)
        return await self.inspect_send(
            self.adapter.state.sticker_operation(ctx.account, ctx.thread, ctx.message_id, "send")
        )
