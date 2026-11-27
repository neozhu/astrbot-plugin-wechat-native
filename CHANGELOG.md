# Changelog

## 0.3.25 — 2026-11-22

- Choose the GIF sticker gear from the source's own shape instead of trial-encoding every ladder step: a measured bytes-per-pixel budget per palette size predicts the output and the first gear that fits is encoded once, with a single cheapest fallback. Auto now uses five gears, conservative seven.
- Drop frames in auto mode when even the smallest gear is predicted to exceed 1 MiB, and give the two smallest palettes their own budget: without them conservative mode refused every GIF that needed them.
- Fix a timeline bug that let compressed GIFs stay above the 10 s sticker limit when delays were uneven. Kept frames now absorb the delays of the frames they replace, and the whole timeline is scaled into 10 s, so a long animation plays slightly faster instead of losing its ending.
- Bound peak memory while compressing: frames are scaled before they are held, and the total pixel budget is capped, so a large GIF can no longer exhaust the container.
- Retry transient preparation reads instead of discarding a preparation that was already submitted.
- Send the staged bytes when a compressed GIF falls back to an ordinary image, so the payload and the source identity describe the same media.
- Mention in the form hint that compression composites transparency onto white.

## 0.3.24 — 2026-11-22

- Fit oversized GIFs into the sticker limits before preparing them, so chat GIFs above 1 MiB still become animated stickers instead of falling back to a static image. A GIF that already fits is uploaded unchanged and Native keeps its exact bytes.
- Add the `gif_sticker_compression` setting: `auto` shrinks, samples frames and reduces colours; `conservative` only shrinks and reduces colours; `none` leaves the bytes alone. Compression runs off the event loop with a 30 s budget and never fails the send.
- Drop the still-picture and metadata side effects from the `image_quality_original` form hint; they are documented as code comments instead.

## 0.3.23 — 2026-11-22

- Send GIF images through the Native sticker channel so WeChat renders them as animated stickers instead of compressing them into a static picture. Preparation requests `maxEdge: 1024` and sends `source: {kind: "prepared"}`.
- Upload each GIF once and reuse that source for the fallback, so a refused preparation still sends an ordinary image without a second upload.
- Fall back to an ordinary image send whenever preparation is refused, unsupported, times out or the setting is off. Add the `send_gif_as_sticker` setting, on by default.

## 0.3.22 — 2026-11-22

- Add the opt-in `image_quality_original` setting for the robot form. When enabled, image sends carry `imageQuality: "original"` so Native keeps JPEG sources unchanged instead of sending the compressed standard copy.
- Leave the field out when the setting is off, so existing standard-quality submissions keep their request hash and idempotency behavior.

## 0.3.21 — 2026-11-21

- Update the recommended Native version to v2.9.0. New bindings start after the latest event, and existing bindings resume from their saved cursors.
- Read article, music and video link cards in live messages and chat history. Attach available thumbnails to live messages when media is enabled.
- Show validated WeChat service names and error codes for failed logins in the administrator dashboard.

## 0.3.20 — 2026-11-18

- Add opt-in friend management for Native v2.7.1. AstrBot administrators can search by WeChat ID, send requests, list pending incoming requests and accept a selected request in WeChat Native private chat.
- Reuse the read credential for search and listing. Send and accept with the account's Admin Token and durable idempotency keys. Report friendship only after Native confirms it.
- Add `/订阅好友`, `/好友订阅状态` and `/取消订阅好友` with English aliases. Route new requests to the bound administrator private chat through the existing SSE stream. Skip existing requests at subscription time and deduplicate notification attempts across restarts.
- Filter friend tool schemas from ineligible main-agent requests. AstrBot 4.29.3 subagents configured to load all tools may still receive those schemas. The plugin checks administrator and private-chat permissions at execution.

## 0.3.19 — 2026-11-15

- Centralize nested send/tool task ownership so shutdown cancels mention-tool post-send hooks and sticker sync polling without manual task re-registration.
- Share the sticker follow-up suppression rule between result decoration and event sending, preserving failure notices while preventing extra text/TTS after successful or unconfirmed sticker sends.
- Remove unused helpers, obsolete test simulation branches and redundant diagnostic logs. Merge overlapping login and QR safety tests while preserving essential warnings/errors, secret-redaction checks and read-only legacy receipt recovery.

## 0.3.18 — 2026-11-15

- Adapt group-member queries to Native v2.4.2 search: forward `search` on every page instead of filtering only the fetched page, preserving matches across nickname, WeChat ID, remark and group nickname fields.
- Include `nickname` and `alias` in member tool results, keeping missing fields null for older responses. Preserve cursor binding to the account, group, search and page size.
- Update member-search and mention-tool guidance; verify search pagination, Chinese and special characters, changed-query cursor rejection and older response compatibility with targeted tests.

## 0.3.17 — 2026-11-14

- Replace the sticker help/list/download/status commands and `/stickers` alias with one administrator private-chat command: `/表情包同步`.
- Automatically wait for account metadata sync, download every unready added official pack, skip ready packs and summarize successes, failures and unfinished tasks. One failed pack does not stop the rest.
- Keep one sync workflow per adapter, poll each Native job for up to five minutes and preserve original jobs on timeout, uncertain admission or shutdown. Subsequent commands check in-flight receipts without reposting; legacy download intents remain protected.
- Keep Native download errors visible, including upstream media-service HTTP 400 failures. Update SPEC v1.6, usage and configuration guidance together; existing find/send behavior is unchanged.

## 0.3.16 — 2026-11-14

- Add opt-in Native v2.4.0/v2.4.1 quoted sticker previews through AstrBot's existing visual pipeline.
- Prepare the sticker library in WeChat administrator private chat with `/表情包`: help, sync, paginated added-pack list, whole-pack download and retained-job status, using the Native HTTP API directly.
- Persist library-operation keys and jobs in the existing journal with an additive in-flight unique index; concurrent commands and restart recovery reuse retained operations without resubmission. Pack membership is still added on the phone.
- Remove `wechat_save_quoted_sticker` and silent group auto-save; quoted sticker previews remain available without importing them into Saved.
- Store local sticker descriptions and `image/gif` readiness cache in SQLite `sticker_labels`, retaining bounded VLM preview labeling for unlabeled cached Saved items when `enable_media` is enabled.
- Restrict `wechat_find_stickers` and `wechat_send_sticker` to locally ready `image/gif` Saved (`GET /custom-stickers/{id}`) and added-pack entries without calling `POST /sticker-downloads`.
- Retain fixed idempotency keys, Job IDs and existing SQLite state, including read-only recovery of legacy save jobs; unknown outcomes block resubmission.
- Keep stickers disabled by default. Back up persistent state before upgrade; see `docs/SPEC-WeChat Native Sticker Receive Save  Send.md` and `docs/verification-stickers-v2.4.0.md` for details.

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
