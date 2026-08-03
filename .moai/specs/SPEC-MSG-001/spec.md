---
id: SPEC-MSG-001
version: 0.2.0
status: draft
created: 2026-05-04
updated: 2026-05-04
author: hchsa77@gmail.com
priority: P0
issue_number: null
---

# SPEC-MSG-001: Messenger Channel Integration (Telegram P0, iMessage P3)

## HISTORY

- 2026-05-04 (v0.2.0): Channel pivot from BlueBubbles iMessage to Telegram Bot API.
  Reasons: Apple ID provisioning blocked (phone duplicate); Twilio A2P 10DLC ≥ 2 weeks;
  Telegram Bot creates in 5 min, free, supports MMS-equivalent and inline keyboards.
  Added link-resolution requirement (Pinterest P0, Instagram P2) after observing
  Telegram delivers only the URL text on Instagram share and a link-preview card on
  Pinterest share. Added AWAITING_IMAGE_PICK state for future multi-image Instagram
  carousel support (deferred to P2 — not implemented in 5/7 demo). BlueBubbles
  demoted from P0 to P3 (post-IR consideration only).
- 2026-05-04 (v0.1.0): Initial draft. Formalizes the architecture agreed in the 2026-05-04 design meeting. P0 = BlueBubbles (self-hosted iMessage relay) for the 2026-05-07 IR demo. P2 = Telegram adapter stub. Adapter pattern from day 1 to keep backend-swap cost near zero.

---

## Goal

Enable a user to send a fashion photo (with optional text or Pinterest link) via a chat channel (Telegram first; iMessage (BlueBubbles) deferred to post-IR P3) to a kiko.ai bot, and receive 4–5 recommended product cards back in-channel. The bot conversation is the demo surface for a 20-second English IR pitch video targeted at D2SF (YC-style) by 2026-05-07.

The AI server (this project, `kikoai/ai`) is the orchestration brain: it ingests inbound channel events, runs vision extraction, drives the existing search pipeline (`app/pipeline/runner.py`), and replies through the channel adapter. Channel transport (Telegram Bot API at `https://api.telegram.org`) is operated by Telegram itself — this SPEC treats it as a black box reachable over HTTPS, authenticated by `TELEGRAM_BOT_TOKEN` and a per-deploy `TELEGRAM_WEBHOOK_SECRET`.

## Non-Goals

- **Persistent session storage** (Redis, Postgres). In-memory `dict + asyncio.Lock` with 30-min TTL is sufficient for the demo and is explicitly deferred to post-demo.
- **Multi-worker / horizontal scaling** of the AI server. The single-worker assumption is accepted for the demo timeline.
- **Telegram group chats, channels, inline-mode, payments, Stars, Mini Apps.** 1:1 DM with the bot only.
- **End-user authentication or account linking.** The sender's Telegram `chat_id` is the only identity used; no mapping to Supabase Auth users in this SPEC.
- **Outbound rate limiting, throttling, or anti-spam.** Not required at demo scale.
- **Re-engineering of the existing search pipeline.** `app/pipeline/runner.py` is reused as-is.
- **iMessage / SMS adapters in P0.** BlueBubbles, Sendblue, LoopMessage are P3 stubs only — adapter pattern leaves the door open; implementations are post-IR.
- **Full Instagram support (multi-image carousel picker, login-gated content).** Single-image Instagram posts may work via P2 stub but are NOT a 5/7 demo deliverable.
- **iMessage / SMS / WhatsApp adapters in P0.** Adapter pattern leaves the door open; implementations are post-IR.

## Stakeholders

| Role | Responsibility |
|------|----------------|
| Product / Founder (hchsa77@gmail.com) | IR pitch owner; final demo content; intent-prompt copy approval |
| AI Server Owner (this SPEC) | `app/channels/`, webhook routing, scenario state machine, vision call, pipeline glue, observability |
| Infra / Bot Operator | Telegram bot ownership (`@kiko_fashion_ai_bot`), webhook URL registration via `setWebhook`, secret rotation, post-demo token revocation |
| kikoai/app (Next.js) team | Out of scope for this SPEC; no changes required |
| Modal team | Out of scope; existing `/embed` endpoint reused unchanged |

---

## Architecture Snapshot (informative)

```
[Instagram / Pinterest user] ──share link──► [Telegram client]
                                                    │ HTTPS POST
                                                    ▼
                                  Telegram Bot API webhook
                                  (X-Telegram-Bot-Api-Secret-Token)
                                                    │
                                                    ▼
                                  app/api/webhooks/telegram.py
                                                    │
                                                    ▼
                                  app/channels/telegram/webhook.py (parse Update)
                                                    │
                                                    ▼
                            ┌───────────────────────┴──────────────────┐
                            ▼                                          ▼
            text contains URL → app/channels/link_resolver.py    photo attached
                            │     ├─ pinterest.com / pin.it (P0)        │
                            │     ├─ instagram.com (P2)                 │
                            │     └─ generic og:image fallback (P0)     │
                            ▼                                          ▼
                            └────────────────► VISION_PROCESSING ◄─────┘
                                                    │
                                                    ▼
                                  app/channels/scenario.py (state machine)
                                                    │
                                                    ▼
                                  app/pipeline/runner.py (UNCHANGED)
                                                    │
                                                    ▼
                                  app/channels/telegram/adapter.py
                                  (sendMessage / sendPhoto / InlineKeyboard)
```

Backend selection is controlled by `MESSENGER_BACKEND` (`telegram` (P0) | `bluebubbles` (P3 stub) | `sendblue` (P3 stub), default `telegram`). All adapters implement the same `MessengerAdapter` ABC (REQ-MSG-008).

---

## Module Layout (informative — implementation detail belongs in plan.md)

```
app/channels/
├── __init__.py
├── adapter.py              # MessengerAdapter ABC
├── schemas.py              # ChannelMessage, BotReply (Pydantic v2)
├── session.py              # in-memory session store + 30-min TTL + asyncio.Lock
├── scenario.py             # 7-state machine (REQ-MSG-005)
├── vision.py               # GPT-4o-mini via LITELLM_BASE_URL
├── link_resolver.py        # pin.it / pinterest.com / generic og:image (P0); instagram via kikoai/app (P2)
├── telegram/               # P0 — fully wired
│   ├── __init__.py
│   ├── adapter.py          # TelegramAdapter(MessengerAdapter)
│   └── webhook.py          # Update payload parser + secret-token verify
├── bluebubbles/            # P3 — stub only (NotImplementedError)
│   ├── __init__.py
│   └── adapter.py
└── sendblue/               # P3 — stub only (NotImplementedError)
    ├── __init__.py
    └── adapter.py

app/api/webhooks/
└── telegram.py             # FastAPI router: POST /webhooks/telegram
```

---

## Scenario State Machine (informative — implementation detail in REQ-MSG-005)

```
IDLE
  ├─ photo attached ─────────────────────────► VISION_PROCESSING
  └─ text(URL) ──► LINK_RESOLUTION
                     ├─ 0 images → reply "Sorry, couldn't load that. Try sharing the photo directly."
                     │             → IDLE
                     ├─ 1 image  → VISION_PROCESSING
                     └─ N images → AWAITING_IMAGE_PICK (P2; in P0 fallback to image[0])
                                     │
                                     └─► VISION_PROCESSING

VISION_PROCESSING ──► AWAITING_INTENT
  bot replies: "Such a cool {item}! What are you looking for — same vibe, something cheaper, or a specific color?"

AWAITING_INTENT ──► SEARCHING (on user text reply)

SEARCHING ──► RESULTS_SENT (4–5 cards via sendPhoto + caption + InlineKeyboard "View" button → product URL)
  closing line: "Tap any to see more like it ✨"

RESULTS_SENT ──► IDLE (on next photo OR 30-min TTL)
```

---

## Requirements (EARS)

### REQ-MSG-001 — Telegram webhook receiver with secret-token verification [P0]

**WHEN** a `POST` request arrives at the Telegram webhook endpoint with header `X-Telegram-Bot-Api-Secret-Token`, **THE SYSTEM SHALL** verify that the header value matches `TELEGRAM_WEBHOOK_SECRET` in constant time and respond with HTTP 200 within 10 s (Telegram retries on timeout).

**Acceptance criteria**:
- Header match → request is processed; HTTP 200 returned within 10 s end-to-end.
- Header missing or mismatched → HTTP 401; the attempt is logged at WARN with source IP.
- Comparison uses `hmac.compare_digest` (constant-time).
- Bot token is NEVER logged or echoed in error responses.

### REQ-MSG-002 — ChannelMessage normalization from Telegram Update payload [P0]

**WHEN** a verified webhook payload is received, **THE SYSTEM SHALL** extract `chat_id`, `from_user`, `text`, `photo[].file_id` (largest size by `file_size`), and `entities[type=url]` from the Telegram Update payload into a `ChannelMessage` Pydantic v2 model with strict types.

**Acceptance criteria**:
- Photo-only message → `photo_file_id` populated with the largest size variant; `text=None`.
- Text-only message → `text` populated; `photo_file_id=None`; URL entities (if any) extracted into `urls: list[HttpUrl]`.
- Photo + caption → both populated.
- Pydantic v2 strict-mode parsing; schema mismatch raises `ChannelParseError` and the webhook responds HTTP 200 (no Telegram retry storm), error logged at ERROR with `chat_id_hash`.
- URL entities pass an SSRF guard mirroring `app/models/request.py` (no localhost, no RFC1918, no link-local).

### REQ-MSG-003 — Link resolver (Pinterest + generic og:image P0; Instagram P2) [P0 / P2]

**WHEN** an inbound message contains a URL entity, **THE SYSTEM SHALL** invoke `app/channels/link_resolver.py::resolve(url)` which returns a `list[str]` of image URLs.

**Acceptance criteria**:
- For `pin.it/*`, follow the redirect to `pinterest.com/pin/{id}`, then parse the `<meta property="og:image">` tag and return `[og_image_url]`.
- For other domains (excluding instagram.com), apply the same generic og:image extraction path.
- For `instagram.com/p/*` and `instagram.com/reel/*`, the P0 implementation returns `[]` (treated as resolution-failed; the bot replies with the polite error message described in REQ-MSG-005). P2 will route to kikoai/app's existing extraction service via HTTP — see REQ-MSG-011.
- HTTP timeout = 8 s per request; on timeout returns `[]`, logged at WARN.
- Returns `list[str]` (may be empty); never raises to the caller.

### REQ-MSG-004 — Vision extraction (image URL or Telegram file_id → keywords) via LiteLLM proxy [P0]

**WHEN** the scenario advances to `VISION_PROCESSING` with either an image URL or a Telegram `file_id`, **THE SYSTEM SHALL** (a) for `file_id`: download the bytes via Telegram `getFile` + file-server URL; (b) call GPT-4o-mini through the existing LiteLLM proxy (`app/providers/llm.py` reused, no new SDK) with a structured-output prompt returning `{"item": str, "color": str, "style": str, "keywords": list[str]}`.

**Acceptance criteria**:
- Successful extraction returns all four fields; `keywords` length ≥ 3 and ≤ 10.
- Vision call timeout = 15 s; on timeout the scenario falls back to `{"item":"item","color":"","style":"","keywords":[]}` and proceeds, logged at WARN.
- LiteLLM 4xx/5xx → same fallback, logged at ERROR with status code.
- Model prompt is fixed in code (not user-controlled) and instructs the model to respond as JSON only.
- Malformed JSON → same fallback (no exception bubbles to webhook handler).
- The vision call is wrapped in a Langfuse `@observe` span (REQ-MSG-009).

### REQ-MSG-005 — Seven-state scenario machine with in-memory session store + 30-min TTL [P0]

**THE SYSTEM SHALL** implement the seven-state machine `IDLE`, `LINK_RESOLUTION`, `AWAITING_IMAGE_PICK` (P2), `VISION_PROCESSING`, `AWAITING_INTENT`, `SEARCHING`, `RESULTS_SENT` per `chat_id`, backed by `dict[chat_id, SessionState]` guarded by `asyncio.Lock`, with each session entry expiring 30 minutes after its last update via a background asyncio task (lazy-eviction acceptable).

**Acceptance criteria**:
- A photo attached in `IDLE` → directly transitions to `VISION_PROCESSING`.
- A text-with-URL inbound in `IDLE` → transitions to `LINK_RESOLUTION`. Resolver returns 0 images → bot replies `"Sorry, couldn't load that. Try sharing the photo directly."` and returns to `IDLE`. Returns 1 image → `VISION_PROCESSING`. Returns N images → in P2 transitions to `AWAITING_IMAGE_PICK`; in P0 falls back to `image[0]` and transitions to `VISION_PROCESSING`.
- `VISION_PROCESSING` auto-advances to `AWAITING_INTENT` after sending the opener `"Such a cool {item}! What are you looking for — same vibe, something cheaper, or a specific color?"`. The `{item}` placeholder is filled from `vision.extract().item`; if empty, the literal string `"piece"` is used.
- `AWAITING_INTENT` advances to `SEARCHING` on inbound user text reply.
- `SEARCHING` auto-advances to `RESULTS_SENT` after the pipeline call (REQ-MSG-007).
- `RESULTS_SENT` returns to `IDLE` on next inbound photo OR after the 30-min TTL.
- A text in `IDLE` with no URL entity → bot replies `"Send me a photo first 📸"` and stays in `IDLE`. Session is NOT created.
- All session reads/writes go through `asyncio.Lock` per `chat_id` to prevent interleaving when two webhook events arrive within the same event-loop tick.
- Bot opener and closer are constants in code; both MUST be English (`BOT_LANGUAGE` default `en`).
- Single-worker assumption is documented inline in the session module docstring; multi-worker support is out of scope.

### REQ-MSG-006 — Telegram outbound adapter (sendMessage / sendPhoto / InlineKeyboardMarkup) [P0]

**WHEN** the scenario calls the adapter's `send_text(chat_id, text)` or `send_card(chat_id, image_url, caption, button_text, button_url)`, **THE SYSTEM SHALL** translate the call into Telegram Bot API requests using httpx (async, no telegram SDK): `sendMessage` for text, `sendPhoto` for cards (image URL + caption + `InlineKeyboardMarkup` with a single `url` button labeled `"View"`).

**Acceptance criteria**:
- `send_text` posts a single Telegram `sendMessage` to `chat_id`.
- `send_card` posts a `sendPhoto` with `caption` and `reply_markup` containing `InlineKeyboardMarkup` with one `url` button (`text="View"`, `url=button_url`).
- Cards within a single recommendation set are sent sequentially to preserve thread order; ordering matches the pipeline's returned product list.
- On HTTP 429, the adapter reads `retry_after` from the response body and backs off accordingly (max 1 retry, then logs ERROR and aborts the remaining cards in that batch).
- All outbound calls are logged at INFO with hashed `chat_id` and elapsed ms; bot token MUST NOT appear in any log line.

### REQ-MSG-007 — Pipeline integration: vision keywords + user intent → existing runner → top 4–5 cards [P0]

**WHEN** the scenario reaches `SEARCHING`, **THE SYSTEM SHALL** merge `{vision_keywords, user_intent_text}` into the existing `RecommendRequest` schema (or its internal equivalent) and invoke `app/pipeline/runner.py` unchanged, then select the top 4–5 results for delivery as cards.

**Acceptance criteria**:
- The pipeline call reuses the existing `PipelineState` / `RecommendRequest` contract — no new fields added.
- Merged query construction: `f"{user_intent_text} {' '.join(vision_keywords)}"` (whitespace-joined, lowercased, trimmed to 256 chars).
- If the pipeline returns < 4 results, the adapter sends whatever is available (minimum 1) and still sends the closing line.
- If the pipeline returns 0 results, the bot replies `"Hmm, I couldn't find a match — try another angle or a different photo."` and the session returns to `IDLE`.
- Each card is built from pipeline result fields: `image_url`, `brand`, `price`, `product_url`. The caption is `f"{brand}\n{price}"`; the InlineKeyboard URL button points to `product_url`.
- End-to-end latency budget (link-resolve + vision + pipeline + outbound) target < 12 s P95 for the demo; informative, not a hard fail; tracked in Langfuse.
- The pipeline call is wrapped in a Langfuse span as a child of the per-session trace (REQ-MSG-009).

### REQ-MSG-008 — MessengerAdapter ABC + MESSENGER_BACKEND env toggle [P0]

**THE SYSTEM SHALL** define `MessengerAdapter` as an abstract base class with the methods needed for inbound parsing and outbound text/card sending, and **WHEN** the FastAPI app starts, **THE SYSTEM SHALL** instantiate exactly one concrete adapter selected by the `MESSENGER_BACKEND` environment variable. Accepted enum values: `telegram` (P0, wired), `bluebubbles` (P3 stub), `sendblue` (P3 stub). Default = `telegram`.

**Acceptance criteria**:
- `MESSENGER_BACKEND=telegram` (default) → `TelegramAdapter` is wired and fully operational.
- `MESSENGER_BACKEND=bluebubbles` → adapter class exists but raises `NotImplementedError("bluebubbles adapter is a P3 stub")` from all methods.
- `MESSENGER_BACKEND=sendblue` → adapter class exists but raises `NotImplementedError("sendblue adapter is a P3 stub")` from all methods.
- Unknown value → app startup fails fast with `ValueError` listing accepted values.
- The selected adapter instance is exposed to FastAPI handlers via a dependency (`get_messenger_adapter`) for testability.
- Adding a new backend requires only: (a) implementing the ABC, (b) registering in the env switch — no changes to `scenario.py` or webhook routing.

### REQ-MSG-009 — Observability: Langfuse @observe wraps the full inbound→reply trace [P0]

**WHEN** a verified webhook is processed, **THE SYSTEM SHALL** open a single Langfuse trace via the existing `app/observability/langfuse.py @observe` wrapper, tagged `channel=telegram` and `chat_id_hash=<sha256(chat_id)[:16]>`, with child spans for inbound parsing, link resolution (when applicable), vision extraction, pipeline runner, and outbound send batch.

**Acceptance criteria**:
- Trace tags include `channel=telegram` and `chat_id_hash` (16-char SHA-256 prefix).
- Raw `chat_id` and bot token MUST NOT appear in any Langfuse field.
- Each child span carries metadata: `state_before`, `state_after`, and (for vision/pipeline) elapsed ms.
- If `LANGFUSE_*` env vars are unset, the existing no-op fallback applies — no errors raised, no behavior change.

### REQ-MSG-010 — Health endpoint extension: /health/ready reports Telegram bot status [P0]

**WHEN** `GET /health/ready` is called (existing endpoint, requires `X-Internal-Token`), **THE SYSTEM SHALL** call Telegram `getMe` and include in the JSON response: `{"messenger_backend": "telegram", "bot_username": "kiko_fashion_ai_bot", "reachable": <bool>}`.

**Acceptance criteria**:
- `reachable = True` iff `getMe` returns HTTP 200 with `ok: true` within 3 s; `False` on timeout/error. The check MUST NOT block the rest of `/health/ready` longer than 3 s.
- `bot_username` is read from the `getMe` response when available; falls back to the configured value otherwise.
- The `/health` (liveness) endpoint is unchanged and does NOT call Telegram.
- For `MESSENGER_BACKEND=bluebubbles` or `sendblue` (P3 stubs), the response reports `messenger_backend` accordingly and `reachable=false`, with no exception raised.

### REQ-MSG-011 — Instagram resolver via kikoai/app extraction service [P2]

**WHEN** an `instagram.com/p/*` or `instagram.com/reel/*` URL is received and the P2 path is enabled, **THE SYSTEM SHALL** make an HTTP call to kikoai/app's existing Instagram extraction route `POST /api/instagram/fetch-post` (file: `kikoai/app/src/app/api/instagram/fetch-post/route.ts`, request `{input: <url-or-shortcode>}`, response includes `slides[].r2Url`/`slides[].originalUrl`) and return `list[str]` of image URLs from the post (prefer `r2Url`, fall back to `originalUrl`).

**Acceptance criteria**:
- Out of scope for the 5/7 demo; documented as a P2 hand-off only.
- Returns `list[str]` (may be empty); never raises to the caller.
- Endpoint URL is configurable via env `PORTAL_APP_BASE_URL` (default `http://localhost:3000` for local dev).
- The route is backed by Apify actor `apify~instagram-post-scraper` with Supabase caching; cold-fetch latency may exceed 60 s — caller MUST set timeout ≥ 90 s and surface an Apify-failure as `[]` (the bot then replies with the standard "Sorry, couldn't load that…" message per REQ-MSG-005).
- Image picker (REQ-MSG-012) is the natural follow-up when `slides.length > 1`; in P0 fallback path the resolver returns only `slides[0]`.
- This requirement is **non-blocking** for the 2026-05-07 demo. P0 ship criteria do not check this REQ.

### REQ-MSG-012 — Image picker UI (multi-image carousel) [P2]

**WHEN** the link resolver returns more than one image, **THE SYSTEM SHALL** send numbered thumbnails as a Telegram media group plus an `InlineKeyboardMarkup` with buttons labeled `[1]`, `[2]`, `[3]`, etc.; on user tap, the `callback_data` drives the state machine to `VISION_PROCESSING` with the selected image.

**Acceptance criteria**:
- Out of scope for the 5/7 demo; documented as a P2 deliverable only.
- In P0, the multi-image case falls back to `image[0]` (REQ-MSG-005).
- This requirement is **non-blocking** for the 2026-05-07 demo. P0 ship criteria do not check this REQ.

---

## Environment Variables (introduced by this SPEC)

| Var | Required | Default | Description |
|-----|----------|---------|-------------|
| `MESSENGER_BACKEND` | no | `telegram` | Adapter selector. Accepted: `telegram` (P0), `bluebubbles` (P3 stub), `sendblue` (P3 stub). |
| `TELEGRAM_BOT_TOKEN` | yes | — | Telegram Bot API token, format `<int>:<base64>`. Issued by `@BotFather`. |
| `TELEGRAM_WEBHOOK_SECRET` | yes | — | 32+ url-safe-character secret echoed back in `X-Telegram-Bot-Api-Secret-Token` (REQ-MSG-001). |
| `TELEGRAM_API_BASE` | no | `https://api.telegram.org` | Telegram Bot API base URL (overridable for local testing or self-hosted Bot API). |
| `VISION_MODEL` | no | `gpt-4o-mini` | Model id passed to LiteLLM proxy for vision extraction. |
| `BOT_LANGUAGE` | no | `en` | Bot reply language. P0 SHALL only support `en`. |

All values live in `.env` for the demo (POC stance per project README); production migration to Parameter Store is out of scope for this SPEC.

---

## Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|------------|
| R1 | **Telegram bot token leakage.** Token was once shared in plaintext during design discussion; could grant full bot control if intercepted. | Medium | High | Revoke and regenerate via `@BotFather` `/revoke` after demo (post-2026-05-08). Token MUST NOT appear in logs (REQ-MSG-006). Pre-commit secret scan covers `TELEGRAM_BOT_TOKEN`. |
| R2 | **Instagram silent failure.** Telegram delivers only the URL text on Instagram share with no preview; users may not understand why the bot can't process it. | High (during demo) | Medium | Explicit error message directing to "share the photo directly or use Pinterest" (REQ-MSG-005 acceptance for 0-image resolver result). Document Instagram limitation in demo script. |
| R3 | **Pinterest `pin.it` short URL rate limiting.** Repeated resolver calls during testing or rehearsal could trip Pinterest's anti-scrape. | Low | Medium | Cache resolved `pin.it` → `og:image` mapping for 1 h in-memory (REQ-MSG-003). |
| R4 | **Webhook URL exposure.** The Telegram webhook is publicly callable; the secret-token header is the only auth boundary. | Medium | High | Fail-closed on missing/wrong header (REQ-MSG-001). Log all rejection attempts at WARN with source IP. Rotate `TELEGRAM_WEBHOOK_SECRET` on incident. |
| R5 | **30-min TTL session loss during demo retake.** If the user pauses > 30 min between shots, the in-flight session is dropped and the demo recording must restart from photo upload. | Medium | Medium | Bump TTL to 60 min for demo via env override; revert post-demo. Document the override knob in `session.py`. |
| R6 | **Single-worker, in-memory session store.** Any restart of the AI server wipes all in-flight conversations. Two uvicorn workers would split sessions across processes and break the state machine. | High (restarts) | Medium | Run uvicorn with `--workers 1` for the demo. Document inline in the session module. Post-demo plan: migrate to Redis (out of scope here). |
| R7 | **Telegram `getUpdates` vs webhook conflict.** If anyone polls `getUpdates` (e.g. local dev) while the webhook is set in production, both break silently. | Medium | High | Explicit `setWebhook` on app startup. Log a WARN if `getWebhookInfo` returns a non-zero `pending_update_count` or a conflicting URL. |
| R8 | **Vision model returns junk / wrong language.** GPT-4o-mini may misclassify or respond in Korean for a Korean caption, breaking the bot opener. | Medium | Medium | Hard-code English-only system prompt. Tolerate malformed JSON (REQ-MSG-004 fallback). Manually rehearse with the exact demo photo before recording. |
| R9 | **Pipeline latency > 12 s blows the demo pacing.** | Medium | Medium | Pipeline reuse means latency is bounded by existing v5 search behavior (already tuned). Pre-warm Modal `/embed` before recording. Track P95 in Langfuse (REQ-MSG-009). |
| R10 | **Image URL SSRF.** A malicious sender could craft an attachment or shared URL pointing to internal infra. | Low (closed bot) | High | Reuse the SSRF guard from `app/models/request.py` (REQ-MSG-002, REQ-MSG-003). |
| R11 | **Demo deadline slip (2026-05-07).** Only ~3 days from this SPEC to the recording. | Medium | High | P0 scope intentionally minimal (no Instagram, no image picker, no persistence). REQ-MSG-011 / REQ-MSG-012 explicitly non-blocking. Daily check-in against this REQ list. |
| R12 | **PII in observability.** Raw `chat_id` or sender info in Langfuse violates privacy expectations. | Medium | Medium | Hash `chat_id` before use as Langfuse tag; never log raw `chat_id` or bot token (REQ-MSG-009). |

---

## Exclusions (What NOT to Build)

The following are explicitly out of scope for SPEC-MSG-001 and MUST NOT be implemented as part of this SPEC:

1. **Persistent session storage** (Redis / Postgres / SQLite). In-memory only. Deferred to a post-demo SPEC.
2. **Multi-worker uvicorn deployment.** Single worker only.
3. **Telegram production code** (webhook handler, real `send_text`/`send_card` bodies). Skeleton stub only per REQ-MSG-008.
4. **Sendblue / LoopMessage SaaS adapters.** `MESSENGER_BACKEND=sendblue` startup-fails by design.
5. **Telegram group chats, channels, inline-mode, payments, Stars, Mini Apps, business-account features.** 1:1 DM only.
6. **End-user authentication or Supabase Auth account linking.** Sender handle is the only identity.
7. **Outbound rate limiting / anti-spam / per-user quotas.**
8. **Localized bot copy.** English only (`BOT_LANGUAGE=en`); other languages are a post-demo concern.
9. **Modifications to `app/pipeline/runner.py`, `app/providers/`, or `app/api/recommend.py`.** Reuse only.
10. **Web UI / admin console for monitoring conversations.** Langfuse UI is sufficient.
11. **BlueBubbles / Mac host operations** (Mac watchdog, Apple ID provisioning, Cloudflare Tunnel, BlueBubbles install). Deferred to P3; not part of this SPEC.
12. **Strict env validation for P3 stubs.** `MESSENGER_BACKEND=bluebubbles|sendblue` MUST NOT require their respective env vars at startup; they fail only when methods are invoked.
13. **Telegram inline-mode (`@bot query`), Telegram payments, Telegram Stars, Telegram Mini Apps.** None of these are part of P0 scope.
14. **Caching layer for resolved og:images beyond the simple 1 h in-memory dict.** Persistent or distributed caches are deferred.

---

## Open Questions (to resolve during plan.md / implementation)

These do not block SPEC approval but should be answered before code is written:

1. Telegram `getFile` returns a time-limited path under `https://api.telegram.org/file/bot<token>/<path>`; can this URL be passed directly to Modal `/embed`, or must we download first and re-upload to R2? (P0 plan: pass the URL directly. Verify with one `/embed` round-trip during implementation. If Modal can't fetch behind the bot-token URL, fall back to download-bytes-then-data-URL or R2 mirror.)
2. Pipeline returns top-15 — should the bot send strictly the top 4 ordered, or apply a small re-rank for visual diversity in the chat? P0 default: top-4 ordered (no re-rank).
3. Should the bot acknowledge inbound photo before vision/pipeline finishes (Telegram `sendChatAction("typing")` or a "Looking…" pre-reply), or stay silent until the opener? P0 default: send `sendChatAction("typing")` once, no pre-text.

## Future Scope (post-demo, separate SPEC)

- **Waitlist mode (post-IR, pre-launch).** When the bot is not yet open to public traffic, any inbound message (e.g., `hello`, link share, photo) MUST trigger a polite "service is still in development — we'll reach out when ready" reply, AND persist the sender's `chat_id` + `username` + first-touch timestamp to a waitlist store (Supabase table TBD). A single env flag (`BOT_MODE=waitlist|live`, default `live`) toggles between this and the demo/scenario flow. Out of scope for SPEC-MSG-001; tracked here so the next SPEC inherits the requirement.
- **Direct photo upload path** (Telegram `getFile` bytes → search). Currently dead-ends at the search step because the pipeline requires `image_url`. Resolution options live in Open Question 1 above; will be picked up in a follow-up SPEC after 5/7 demo.

---

## Cross-References

- Existing pipeline contract: `app/pipeline/runner.py`, `app/pipeline/state.py` (reused unchanged).
- LiteLLM proxy contract: `app/providers/llm.py` (vision call follows same `LITELLM_BASE_URL` pattern).
- Auth / health pattern: `app/core/auth.py`, `app/api/health.py` (REQ-MSG-010 extends `/health/ready`).
- Observability pattern: `app/observability/langfuse.py` (REQ-MSG-009 reuses `@observe`).
- Project context: `/Users/hansangho/Desktop/kikoai/ai/CLAUDE.md` — AI server is stateless and reuses LiteLLM + Modal + Supabase.
- Infra ownership: `/Users/hansangho/Desktop/aws-infra/kiko-ai-servers/portal-ai/` (EC2 docker-compose; webhook URL registration via `setWebhook` is performed at app startup, not via infra repo).
- Instagram extraction reuse: `kikoai/app/src/app/api/instagram/fetch-post/route.ts` (POST, request `{input}`, response `{slides: [{r2Url, originalUrl, ...}]}`), backed by `apify~instagram-post-scraper` actor with Supabase cache (REQ-MSG-011).

---

## Definition of Done (P0, demo-blocking)

- [ ] REQ-MSG-001 through REQ-MSG-010 implemented and acceptance criteria verified.
- [ ] REQ-MSG-011 (Instagram resolver) and REQ-MSG-012 (image picker UI) deferred to P2; class-shells / TODO markers present but full implementation NOT required.
- [ ] Real Telegram bot (`@kiko_fashion_ai_bot`) replies end-to-end to a Pinterest pin link with 4–5 product cards including images and View buttons, captured in screen-recording demo.
- [ ] Link resolver returns ≥ 1 image URL for 5 sample Pinterest pins (mix of `pin.it` short links and full `pinterest.com/pin/...` URLs).
- [ ] End-to-end manual test: photo + text → 4 cards back, on a real Telegram client, recorded latency < 15 s for the recorded turn.
- [ ] `/health/ready` returns `reachable=true` for the Telegram bot immediately before demo recording.
- [ ] Langfuse trace for the recorded turn is visible with all child spans (parse, optional link-resolve, vision, pipeline, send-batch).
- [ ] No secrets in committed code (`.env.example` updated with placeholder Telegram vars).
- [ ] `ruff check . && ruff format --check .` passes.
