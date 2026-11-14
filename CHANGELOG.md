# Changelog

## 0.3.15 — 2026-11-14

- Add opt-in Native v2.4.0/v2.4.1 quoted sticker previews through AstrBot's existing visual pipeline.
- Add quoted sticker Saved import (`wechat_save_quoted_sticker` and silent group `auto_save`) with verified original-byte MD5 deduplication, SQLite concurrent claims and strict server/writeback receipts.
- Store local sticker descriptions and `image/gif` readiness cache in SQLite `sticker_labels` (via `wechat_save_quoted_sticker(caption=...)` and VLM preview labeling when `enable_media` is enabled), without altering Native/WeChat payloads.
- Restrict `wechat_find_stickers` and `wechat_send_sticker` to locally ready `image/gif` Saved (`GET /custom-stickers/{id}`) and added-pack entries without calling `POST /sticker-downloads`.
- Retain fixed idempotency keys and Job IDs; restart recovery only reads original receipts, and unknown outcomes block resubmission.
- Keep stickers disabled by default. Back up persistent state before upgrade; see `docs/SPEC-WeChat Native Sticker Receive Save  Send.md` and `docs/verification-stickers-v2.4.0.md` for details.

## 0.3.14 — 2026-11-13

- Preserve connection status and offline reasons during automatic refresh and post-login checks to prevent panel flicker and layout jumps.
- Combine offline reasons, QR login guidance and retry controls in one card that hides when the account comes online.
- Register alert commands with Chinese primary names (`绑定通知`, `解绑通知`, `通知状态`, `测试通知`), retaining the existing English names as aliases.

## 0.3.13 — 2026-11-12

- Make explicit QR retries bypass saved-session recovery; persist the request strategy with its idempotency key across timeouts and restarts.
- Remove temporary QR login diagnostic logs after validating the login flow.
- Center the QR login card content on desktop and mobile layouts.
- Add Native v2.3.0 QR login to the existing connection panel, using the existing Admin Token, durable idempotency and validated in-memory PNG transfer.
- Add administrator private-chat alert commands with Chinese aliases and platform-neutral routing through AstrBot.
- Add independent read-only connection monitoring, durable 3-failure / 2-recovery incidents, and at-most-once notification attempts across restarts.
- Preserve the existing message pipeline, statistics and 30-second panel refresh; add mock HTTP, incident, permission and Chromium coverage.
