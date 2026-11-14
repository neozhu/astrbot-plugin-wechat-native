"""Sticker resources tied to the current trusted message and Native account."""

import asyncio
import hashlib
import json
import re
import time
import uuid

from astrbot.api import logger

from .config import MAX_IMAGE_BYTES
from .event import sticker_preview
from .native_client import (
    _SAFE_CODES,
    MediaError,
    NativeAPIError,
    NativeClient,
    SendError,
    _extract_job_error_code,
)
from .state import clean_sticker_caption
from .tool_support import ToolError, context, is_tool_error, result


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


class StickerService:
    def __init__(self, adapter):
        self.adapter = adapter

    def log_quote(self, raw, stage):
        quote = raw.get("quote")
        quote = quote if isinstance(quote, dict) else {}
        state = quote.get("state")
        if state not in {"embedded", "missing", "unsupported", "recalled"}:
            state = "unknown"
        quote_type = quote.get("type")
        app_type = quote.get("appMessageType")
        logger.info(
            "WeChat Native 引用表情诊断：message=%s stage=%s quote_type=%s "
            "quote_app_type=%s quote_state=%s embedded_sticker=%s embedded_text=%s "
            "enable_stickers=%s enable_media=%s",
            raw.get("id"),
            stage,
            quote_type if type(quote_type) is int else None,
            app_type if type(app_type) is int else None,
            state,
            quote.get("embeddedSticker") is True,
            isinstance(quote.get("embeddedText"), str) and bool(quote["embeddedText"]),
            self.adapter.native_config.enable_stickers,
            self.adapter.native_config.enable_media,
        )

    def auto_save_eligible(self, raw, origin="live"):
        cfg = self.adapter.native_config
        thread = raw.get("threadId", "")
        return (
            cfg.enable_stickers
            and cfg.group_reply_mode != "disabled"
            and origin == "live"
            and raw.get("accountId") == cfg.account_id
            and thread.endswith("@chatroom")
            and (not cfg.allowed_groups or thread in cfg.allowed_groups)
            and raw.get("direction") == "incoming"
            and isinstance(raw.get("senderId"), str)
            and bool(raw["senderId"])
            and raw["senderId"] != self.adapter.bot_self_id
            and raw.get("recall") is None
            and quoted_sticker(raw)
            and self.adapter._check_timestamp(raw)
        )

    async def auto_save(self, raw):
        """Silently save a validated live group quote, independently of model wake-up."""
        adapter = self.adapter

        def authorize():
            if (
                adapter.stopped
                or adapter._state_closed
                or adapter.client.closed
                or not self.auto_save_eligible(raw)
                or adapter.loop_guard.blocked(raw["threadId"])
                or adapter.state.message_status(raw["id"]) not in {"queued", "replied", "no_reply"}
                or not adapter.state.message_matches(
                    raw["id"], raw["threadId"], raw.get("serverId")
                )
                or adapter.state.is_recalled(raw["threadId"], raw["quote"].get("serverId"))
            ):
                raise ToolError("not_available", "automatic_sticker_source_invalidated")

        try:
            labeler = None
            bound = getattr(adapter, "sticker_labeler", None)
            if callable(bound) and adapter.native_config.enable_media:
                umo = f"{adapter.meta().id}:GroupMessage:{raw['threadId']}"

                async def auto_label(preview_png: bytes) -> str:
                    return await bound(preview_png, umo)

                labeler = auto_label
            async with adapter._media_semaphore:
                outcome = await self._save_quote(raw, authorize, labeler=labeler)
            logger.info(
                "WeChat Native 引用表情自动收藏：message=%s status=%s",
                raw["id"],
                outcome["status"],
            )
        except Exception as error:
            logger.warning(
                "WeChat Native 引用表情自动收藏未完成：message=%s error_type=%s",
                raw["id"],
                type(error).__name__,
            )

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

    def log_acquisition(self, row, stage, job=None, error=None):
        """Log only receipt identity, known enums/codes and comparison booleans."""
        job = job if isinstance(job, dict) else {}
        native_state, phase = job.get("state"), job.get("phase")
        reason = job.get("reason")
        code = (
            reason
            if isinstance(reason, str) and reason in _SAFE_CODES
            else _extract_job_error_code(job)
        )
        native_state = (
            native_state
            if isinstance(native_state, str)
            and native_state
            in {
                "queued",
                "running",
                "available",
                "needs_review",
                "failed",
                "cancelled",
                "expired",
                "unsupported",
            }
            else "unknown"
        )
        phase = (
            phase
            if isinstance(phase, str)
            and phase
            in {
                "queued",
                "preparing",
                "downloading",
                "reserving",
                "uploading",
                "submitting",
                "readback",
                "projecting",
                "completed",
            }
            else "unknown"
        )
        code = code if isinstance(code, str) and code in _SAFE_CODES else "unknown"
        identity_matches = (
            job.get("accountId") == row["account"]
            and job.get("kind") == "sticker_acquire"
            and isinstance(job.get("id"), str)
            and bool(job["id"])
            and (not row["job_id"] or job["id"] == row["job_id"])
        )
        payload = job.get("result")
        payload = payload if isinstance(payload, dict) else {}
        acceptance = payload.get("serverAcceptance")
        acceptance = (
            acceptance
            if isinstance(acceptance, str)
            and acceptance
            in {
                "pending",
                "accepted",
                "not_attempted",
                "rejected",
                "reconciled_noop",
            }
            else "unknown"
        )
        logger.warning(
            "WeChat Native 表情获取诊断：stage=%s http=%s code=%s "
            "receipt_key=%s job_id=%s identity_matches=%s native_state=%s "
            "native_phase=%s native_code=%s acceptance=%s error_type=%s",
            stage,
            error.status if isinstance(error, NativeAPIError) else None,
            error.code if isinstance(error, NativeAPIError) else "none",
            row["idempotency_key"],
            job["id"] if identity_matches else row["job_id"],
            identity_matches,
            native_state,
            phase,
            code,
            acceptance,
            type(error).__name__ if error else "none",
        )

    async def inspect(self, row, job=None):
        action, key = row["action"], row["idempotency_key"]
        acquiring = action.startswith("acquire:")
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
                    if acquiring:
                        self.log_acquisition(row, "missing_receipt")
                    return result(action, state)
                job = await self.adapter.client.job(job_id)
            expected_kind = (
                "sticker_acquire"
                if acquiring
                else "sticker_saved"
                if action == "save"
                else "download"
            )
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
                if acquiring:
                    if (
                        payload.get("packId") == action.removeprefix("acquire:")
                        and payload.get("serverAcceptance") == "accepted"
                        and payload.get("contentReadiness") == "available"
                        and isinstance(payload.get("items"), list)
                        and any(self.pack_original_ready(a) for a in payload["items"])
                    ):
                        state = "success"
                elif action.startswith("transfer:") or (
                    payload.get("profileWriteback") == "complete"
                    and payload.get("serverAcceptance") in {"accepted", "reconciled_noop"}
                ):
                    state = "success"
            elif job.get("state") in {"queued", "running"}:
                state = "pending"
            elif job.get("state") in {"failed", "cancelled", "expired", "unsupported"}:
                if (
                    acquiring
                    or action.startswith("transfer:")
                    or payload.get("serverAcceptance") in {"not_attempted", "rejected"}
                ):
                    state = "failed"
        except (NativeAPIError, ToolError, TypeError, AttributeError) as error:
            if acquiring and isinstance(error, NativeAPIError):
                self.log_acquisition(row, "job_read", error=error)
            state = "needs_review"
        self.adapter.state.finish_sticker(key, state, job_id, server_id)
        if acquiring and state in {"needs_review", "failed"}:
            self.log_acquisition(row, "job_result", job)
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

    @staticmethod
    def _merge_captions(official, local):
        official_clean = clean_sticker_caption(official)
        local_clean = clean_sticker_caption(local)
        if not local_clean:
            return official_clean
        if not official_clean:
            return local_clean
        if local_clean.casefold() in official_clean.casefold():
            return official_clean
        if official_clean.casefold() in local_clean.casefold():
            return local_clean
        return f"{official_clean} {local_clean}"[:160]

    async def _generate_caption(self, labeler, data, *, is_preview=False):
        if not callable(labeler) or not data:
            return ""
        try:
            preview_bytes = data if is_preview else await asyncio.to_thread(sticker_preview, data)
            async with asyncio.timeout(10):
                return clean_sticker_caption(await labeler(preview_bytes))
        except Exception:
            return ""

    async def _record_saved_label(
        self,
        content_id,
        asset,
        *,
        caption="",
        official_caption="",
        data=None,
        labeler=None,
    ):
        account = self.adapter.native_config.account_id
        content_type = str(asset.get("contentType") or "") if isinstance(asset, dict) else ""
        ready_gif = content_type == "image/gif"
        local_caption = clean_sticker_caption(caption)
        if not local_caption and labeler and data:
            existing = self.adapter.state.sticker_label(account, content_id)
            if not (existing and existing["caption"]) and not clean_sticker_caption(
                official_caption
            ):
                local_caption = await self._generate_caption(labeler, data)
        self.adapter.state.upsert_sticker_label(
            account,
            content_id,
            caption=local_caption or None,
            ready_gif=ready_gif,
            content_type=content_type,
        )

    async def save(self, event, caption="", *, labeler=None):
        if not isinstance(caption, str):
            raise ToolError("invalid_request")
        return await self._save_quote(
            event.message_obj.raw_message,
            lambda: context(event, stickers=True),
            caption=caption,
            labeler=labeler,
        )

    async def _save_quote(self, raw, authorize, *, caption="", labeler=None):
        authorize()
        clean_caption = clean_sticker_caption(caption)
        self.log_quote(raw, "save")
        existing = self.adapter.state.sticker_operation(
            raw["accountId"], raw["threadId"], raw["id"], "save"
        )
        if existing:
            outcome = await self.inspect(existing)
            if clean_caption and outcome["status"] in {"success", "already_saved"}:
                self.adapter.state.upsert_sticker_label(
                    self.adapter.native_config.account_id,
                    existing["content_id"],
                    caption=clean_caption,
                )
            return outcome
        if not quoted_sticker(raw):
            quote = raw.get("quote")
            reason = (
                "sticker_original_recalled"
                if isinstance(quote, dict) and quote.get("state") == "recalled"
                else "sticker_quote_unavailable"
            )
            raise ToolError("not_available", reason)
        try:
            data, asset = await self.read_quote(raw, original=True)
            content_id = hashlib.md5(data).hexdigest()
            saved = await self.adapter.client.custom_stickers()
            if not isinstance(saved.get("items"), list):
                raise ToolError("not_available")
            await self.check_quote(raw)
            authorize()
        except MediaError as error:
            raise ToolError("not_available", "sticker_original_" + error.reason) from None
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
            outcome = await self.inspect(row)
            if clean_caption and outcome["status"] in {"success", "already_saved"}:
                self.adapter.state.upsert_sticker_label(
                    self.adapter.native_config.account_id,
                    row["content_id"],
                    caption=clean_caption,
                )
            return outcome
        key = row["idempotency_key"]
        saved_match = next(
            (
                s
                for s in saved["items"]
                if isinstance(s, dict) and str(s.get("id", "")).lower() == content_id
            ),
            None,
        )
        if saved_match is not None:
            self.adapter.state.finish_sticker(key, "already_saved")
            await self._record_saved_label(
                content_id,
                asset,
                caption=clean_caption,
                official_caption=saved_match.get("caption") or "",
                data=data,
                labeler=labeler,
            )
            return result("save", "already_saved")
        try:
            job = await self.adapter.client.save_sticker(raw["id"], asset["id"], key)
            outcome = await self.inspect(row, job)
            if outcome["status"] == "success":
                await self._record_saved_label(
                    content_id,
                    asset,
                    caption=clean_caption,
                    data=data,
                    labeler=labeler,
                )
            return outcome
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

    async def _saved_original(self, item, label_row, *, allow_detail=True):
        account = self.adapter.native_config.account_id
        sid = item["id"]
        inline = item.get("original")
        if isinstance(inline, dict):
            ready = self.original_gif_ready(inline)
            self.adapter.state.upsert_sticker_label(
                account,
                sid,
                ready_gif=ready,
                content_type=str(inline.get("contentType") or ""),
            )
            return ready, inline, False
        if label_row and label_row["ready_gif"] == 1:
            return True, None, False
        if not allow_detail:
            return False, None, False
        try:
            detail = await NativeClient.custom_sticker(self.adapter.client, sid)
        except NativeAPIError as error:
            if error.status in {401, 403}:
                raise
            return False, None, True
        original = detail.get("original") if isinstance(detail, dict) else None
        ready = self.original_gif_ready(original)
        self.adapter.state.upsert_sticker_label(
            account,
            sid,
            ready_gif=ready,
            content_type=str(original.get("contentType") or "")
            if isinstance(original, dict)
            else "",
        )
        return ready, original, True

    async def _autolabel_saved(self, sid, original, labeler):
        if not callable(labeler):
            return ""
        try:
            if original is None:
                detail = await NativeClient.custom_sticker(self.adapter.client, sid)
                original = detail.get("original") if isinstance(detail, dict) else None
            if not self.original_gif_ready(original):
                return ""
            has_preview = isinstance(original.get("preview"), dict)
            content = await NativeClient.sticker_content(
                self.adapter.client, original, original=not has_preview
            )
        except (MediaError, NativeAPIError):
            return ""
        generated = await self._generate_caption(labeler, content, is_preview=has_preview)
        if generated:
            self.adapter.state.upsert_sticker_label(
                self.adapter.native_config.account_id,
                sid,
                caption=generated,
                ready_gif=True,
                content_type="image/gif",
            )
        return generated

    async def catalog(self, *, labeler=None, label_missing=False):
        saved = await self.adapter.client.custom_stickers()
        packs = await self.adapter.client.sticker_packs()
        if not isinstance(saved.get("items"), list) or not isinstance(packs.get("items"), list):
            raise ToolError("not_available")
        account = self.adapter.native_config.account_id
        labels = self.adapter.state.sticker_labels(account)
        items = []
        detail_budget = 50
        label_budget = 5 if label_missing and callable(labeler) else 0
        saved_ready = 0
        for item in saved["items"][:100]:
            if not isinstance(item, dict) or not re.fullmatch(
                r"[0-9a-fA-F]{32}", str(item.get("id", ""))
            ):
                continue
            sid = item["id"]
            label_row = labels.get(sid.lower())
            ready, original, fetched = await self._saved_original(
                item, label_row, allow_detail=detail_budget > 0
            )
            if fetched:
                detail_budget -= 1
            if not ready:
                continue
            saved_ready += 1
            caption = self._merge_captions(
                item.get("caption") or "",
                label_row["caption"] if label_row else "",
            )
            if not caption and label_budget > 0:
                label_budget -= 1
                generated = await self._autolabel_saved(sid, original, labeler)
                if generated:
                    caption = self._merge_captions(item.get("caption") or "", generated)
            items.append(
                {
                    "source": {"kind": "saved", "stickerId": sid},
                    "caption": caption,
                    "pack_name": "",
                }
            )
        # Bound metadata work; include only locally ready official GIFs (no /sticker-downloads).
        pack_ready = 0
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
                if not self.original_gif_ready(item.get("original")):
                    continue
                pack_ready += 1
                name = pack.get("name")
                label_row = labels.get(str(item["id"]).lower())
                caption = self._merge_captions(
                    item.get("caption") or "",
                    label_row["caption"] if label_row else "",
                )
                items.append(
                    {
                        "source": {"kind": "pack", "packId": pack_id, "stickerId": item["id"]},
                        "caption": caption,
                        "pack_name": name if isinstance(name, str) and name != pack_id else "",
                    }
                )
        diagnostics = {
            "source": "native_profile",
            "saved_count": len(saved["items"]),
            "saved_ready_count": saved_ready,
            "added_pack_count": sum(
                isinstance(p, dict) and p.get("membership") == "added" for p in packs["items"]
            ),
            "pack_ready_count": pack_ready,
            "candidate_count": len(items),
            "native_version": None,
        }
        if not items:
            try:
                status = await self.adapter.client.status()
                identity = status.get("version")
                version = identity.get("version") if isinstance(identity, dict) else identity
                if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:-[\w.]+)?", version):
                    diagnostics["native_version"] = version
            except NativeAPIError:
                pass
        logger.info(
            "WeChat Native 表情目录诊断：saved_count=%s added_pack_count=%s saved_ready=%s "
            "pack_ready=%s candidate_count=%s native_version=%s",
            diagnostics["saved_count"],
            diagnostics["added_pack_count"],
            diagnostics["saved_ready_count"],
            diagnostics["pack_ready_count"],
            len(items),
            diagnostics["native_version"] or "unknown",
        )
        return items, diagnostics

    async def find(self, event, query="", limit=12, *, labeler=None):
        ctx = context(event, stickers=True)
        if (
            not isinstance(query, str)
            or len(query.encode()) > 256
            or type(limit) is not int
            or not 1 <= limit <= 12
        ):
            raise ToolError("invalid_request")
        catalog, diagnostics = await self.catalog(labeler=labeler, label_missing=True)
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
        output = result(
            "find",
            "success",
            items=choices,
            count=len(choices),
            untrusted_data=True,
            catalog=diagnostics,
        )
        if not choices:
            output["reason"] = (
                "sticker_query_no_match" if catalog else "native_sticker_catalog_empty"
            )
            output["message"] = (
                "当前目录没有匹配该文字的候选，不能判断此前收藏是否成功。"
                if catalog
                else "Native 当前账号目录未返回本地可用的 GIF 表情，"
                "不能判断手机库存或此前收藏是否成功。"
            )
        return output

    async def validate_selection(self, source):
        if source["kind"] == "saved":
            page = await self.adapter.client.custom_stickers()
            item = next(
                (
                    i
                    for i in page.get("items", [])
                    if isinstance(i, dict) and i.get("id") == source["stickerId"]
                ),
                None,
            )
            if item is None:
                raise ToolError("not_available", "sticker_membership_changed")
            original = item.get("original")
            if not isinstance(original, dict):
                detail = await NativeClient.custom_sticker(self.adapter.client, source["stickerId"])
                original = detail.get("original") if isinstance(detail, dict) else None
            ready = self.original_gif_ready(original)
            self.adapter.state.upsert_sticker_label(
                self.adapter.native_config.account_id,
                source["stickerId"],
                ready_gif=ready,
                content_type=str(original.get("contentType") or "")
                if isinstance(original, dict)
                else "",
            )
            if not ready:
                raise ToolError("not_available", "sticker_saved_original_unavailable")
            return item
        packs = await self.adapter.client.sticker_packs()
        valid = any(
            isinstance(p, dict)
            and p.get("id") == source["packId"]
            and p.get("membership") == "added"
            for p in packs.get("items", [])
        )
        item = None
        if valid:
            page = await self.adapter.client.pack_stickers(source["packId"])
            item = next(
                (
                    i
                    for i in page.get("items", [])
                    if isinstance(i, dict)
                    and i.get("id") == source["stickerId"]
                    and i.get("packId") == source["packId"]
                ),
                None,
            )
            valid = page.get("packId") == source["packId"] and item is not None
        if not valid:
            raise ToolError("not_available", "sticker_membership_changed")
        if not self.original_gif_ready(item.get("original")):
            raise ToolError("not_available", "sticker_pack_original_unavailable")
        return item

    def original_gif_ready(self, asset):
        return (
            isinstance(asset, dict)
            and isinstance(asset.get("id"), str)
            and bool(asset["id"])
            and asset.get("accountId") == self.adapter.native_config.account_id
            and asset.get("messageId") is None
            and asset.get("kind") == "sticker"
            and asset.get("variant") == "original"
            and asset.get("actualVariant") == "original"
            and asset.get("state") == "available"
            and asset.get("contentType") == "image/gif"
            and type(asset.get("byteLength")) is int
            and 0 < asset["byteLength"] <= MAX_IMAGE_BYTES
            and isinstance(asset.get("sha256"), str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", asset["sha256"]) is not None
        )

    def pack_original_ready(self, asset):
        return self.original_gif_ready(asset)

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
        def finish_outcome(outcome):
            if outcome.get("status") in {"success", "pending", "submitting", "needs_review"}:
                event.set_extra("wechat_native_sticker_suppress_followup", True)
            return outcome

        ctx = context(event, stickers=True, sticker_recovery=True)
        existing = self.adapter.state.sticker_operation(
            ctx.account, ctx.thread, ctx.message_id, "send"
        )
        if existing:
            return finish_outcome(await self.inspect_send(existing))
        selections = event.get_extra("wechat_native_sticker_selections") or {}
        selection = selections.get(selection_id) if isinstance(selection_id, str) else None
        if (
            not selection
            or (selection["account"], selection["thread"], selection["trigger"])
            != (ctx.account, ctx.thread, ctx.message_id)
            or time.time() - selection["created"] > 300
        ):
            raise ToolError("invalid_request", "sticker_selection_expired")
        source = selection["source"]
        await self.validate_selection(source)
        context(event, stickers=True)
        raw = event.message_obj.raw_message
        row, won = self.claim(raw, "send", source["stickerId"], source)
        if row is None:
            raise ToolError("rate_limited")
        if not won:
            return finish_outcome(await self.inspect_send(row))
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
            record = self.adapter.state.send_record(key)
            state = (
                "failed"
                if not record
                or record["state"] == "failed"
                or isinstance(error, ValueError)
                or (isinstance(error, SendError) and error.state == "failed")
                else "needs_review"
            )
            self.adapter.state.finish_sticker(key, state)
            if isinstance(error, asyncio.CancelledError) or is_tool_error(error):
                raise
            fields = {}
            if state == "failed" and isinstance(error, NativeAPIError):
                fields = {
                    "reason": error.code,
                    "message": "Native 已明确拒绝这次表情发送，未发送成功。",
                }
            return finish_outcome(result("send", state, **fields))
        return finish_outcome(
            await self.inspect_send(
                self.adapter.state.sticker_operation(
                    ctx.account, ctx.thread, ctx.message_id, "send"
                )
            )
        )
