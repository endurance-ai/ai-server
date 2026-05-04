---
id: SPEC-MSG-001
version: 0.1.0
status: draft
created: 2026-05-04
updated: 2026-05-04
author: hchsa77@gmail.com
priority: P0
issue_number: null
---

# SPEC-MSG-001: Messenger Channel Integration (iMessage P0, Telegram P2)

## HISTORY

- 2026-05-04 (v0.1.0): Initial draft. Formalizes the architecture agreed in the 2026-05-04 design meeting. P0 = BlueBubbles (self-hosted iMessage relay) for the 2026-05-07 IR demo. P2 = Telegram adapter stub. Adapter pattern from day 1 to keep backend-swap cost near zero.

---

## Goal

Enable a user to send a fashion photo (with optional text or Pinterest link) via a chat channel (iMessage first, Telegram later) to a Portal.ai bot, and receive 4–5 recommended product cards back in-channel. The bot conversation is the demo surface for a 20-second English IR pitch video targeted at D2SF (YC-style) by 2026-05-07.

The AI server (this project, `portal/ai`) is the orchestration brain: it ingests inbound channel events, runs vision extraction, drives the existing search pipeline (`app/pipeline/runner.py`), and replies through the channel adapter. Channel transport (BlueBubbles REST + Cloudflare Tunnel + a Mac running Messages.app) is operated externally — this SPEC treats it as a black box reachable over HTTP.

## Non-Goals

- **Persistent session storage** (Redis, Postgres). In-memory `dict + asyncio.Lock` with 30-min TTL is sufficient for the demo and is explicitly deferred to post-demo.
- **Multi-worker / horizontal scaling** of the AI server. The single-worker assumption is accepted for the demo timeline.
- **Production Telegram support.** Only an adapter skeleton (REQ-MSG-008) ships in P2; full implementation is post-demo.
- **iMessage group chats, reactions, tapbacks, read receipts, typing indicators.** 1:1 DM only.
- **End-user authentication or account linking.** The sender's iMessage handle (phone or Apple ID) is the only identity used; no mapping to Supabase Auth users in this SPEC.
- **Outbound rate limiting, throttling, or anti-spam.** Not required at demo scale.
- **Re-engineering of the existing search pipeline.** `app/pipeline/runner.py` is reused as-is.
- **Replacing BlueBubbles with SaaS (Sendblue, LoopMessage).** SaaS backends are documented as fallback only and gated behind `MESSENGER_BACKEND` env; not implemented in P0.

## Stakeholders

| Role | Responsibility |
|------|----------------|
| Product / Founder (hchsa77@gmail.com) | IR pitch owner; final demo content; intent-prompt copy approval |
| AI Server Owner (this SPEC) | `app/channels/`, webhook routing, scenario state machine, vision call, pipeline glue, observability |
| Infra / BlueBubbles Operator | Mac host, Apple ID provisioning, BlueBubbles install, Cloudflare Tunnel, secret rotation |
| portal/app (Next.js) team | Out of scope for this SPEC; no changes required |
| Modal team | Out of scope; existing `/embed` endpoint reused unchanged |

---

## Architecture Snapshot (informative)

```
[iMessage user] ──DM photo+text──► [Mac + Messages.app + BlueBubbles server]
                                          │  (Cloudflare Tunnel)
                                          ▼
                          [POST /webhooks/imessage] (HMAC verified)
                                          │
                                          ▼
                          app/channels/bluebubbles/webhook.py
                                          │
                                          ▼
                          app/channels/adapter.py → ChannelMessage
                                          │
                                          ▼
                          app/channels/scenario.py (6-step state machine)
                                  │              │
                                  ▼              ▼
                          app/channels/vision.py  app/channels/session.py (in-memory)
                                  │ (LiteLLM proxy, gpt-4o-mini)
                                  ▼
                          app/pipeline/runner.py (embed → search → diversify)
                                  │
                                  ▼
                          MessengerAdapter.send_card() × 4–5  +  send_text() closer
                                          │
                                          ▼
                          [BlueBubbles outbound REST] ──► [iMessage user]

Observability: Langfuse @observe wraps the full inbound→reply trace as one session id = sender_handle.
```

Backend selection is controlled by `MESSENGER_BACKEND` (`bluebubbles` | `telegram` | `sendblue`, default `bluebubbles`). All adapters implement the same `MessengerAdapter` ABC (REQ-MSG-007).

---

## Module Layout (informative — implementation detail belongs in plan.md)

```
app/channels/
├── __init__.py
├── adapter.py              # MessengerAdapter ABC
├── schemas.py              # ChannelMessage, BotReply (Pydantic v2)
├── session.py              # in-memory session store + 30-min TTL + asyncio.Lock
├── scenario.py             # 6-step state machine
├── vision.py               # GPT-4o-mini via LITELLM_BASE_URL
├── bluebubbles/
│   ├── __init__.py
│   ├── adapter.py          # BlueBubblesAdapter(MessengerAdapter)
│   └── webhook.py          # parse + HMAC verify helpers
└── telegram/               # P2 — skeleton only
    ├── __init__.py
    └── adapter.py          # TelegramAdapter(MessengerAdapter), NotImplemented body

app/api/webhooks/
└── imessage.py             # FastAPI router: POST /webhooks/imessage
```

---

## Requirements (EARS)

### REQ-MSG-001 — BlueBubbles webhook receiver with HMAC signature verification [P0]

**WHEN** a `POST /webhooks/imessage` request arrives with header `X-BB-Signature` and a JSON body, **THE SYSTEM SHALL** compute `HMAC-SHA256(BLUEBUBBLES_WEBHOOK_SECRET, raw_body)` and accept the request only when the computed digest matches the header value in constant time.

**Acceptance criteria**:
- Request with valid signature → handler proceeds, returns HTTP 200 within 500 ms (ack only; processing is async).
- Request with missing `X-BB-Signature` header → HTTP 401, body `{"detail":"missing signature"}`.
- Request with mismatched signature → HTTP 401, body `{"detail":"invalid signature"}`.
- Request larger than 10 MB → HTTP 413 before HMAC computation.
- Signature comparison uses `hmac.compare_digest` (constant-time); no early exit on first byte mismatch.
- Webhook handler is registered under FastAPI dependency `verify_bluebubbles_signature` and is the only public path under `/webhooks/imessage`.

### REQ-MSG-002 — ChannelMessage normalization (text + image attachments + sender id) [P0]

**WHEN** a verified webhook payload is received, **THE SYSTEM SHALL** parse it via `MessengerAdapter.parse_inbound(payload)` and produce a `ChannelMessage` Pydantic v2 model containing at minimum: `channel` (literal), `sender_handle` (str), `text` (str | None), `image_urls` (list[HttpUrl]), `received_at` (datetime, UTC), `raw_provider_id` (str).

**Acceptance criteria**:
- Photo-only message → `text=None`, `image_urls=[<url>]`, `sender_handle` populated.
- Text-only message → `text="..."`, `image_urls=[]`.
- Photo + caption → both populated.
- Multi-attachment message → only the first image is used in P0; remaining attachments are dropped with a `WARN`-level log entry that includes `raw_provider_id`.
- Non-image attachments (video, audio, vCard) → ignored; logged at INFO.
- Image URLs MUST pass an SSRF guard mirroring the existing `app/models/request.py` rule (no `localhost`, no RFC1918, no link-local).
- Schema mismatch (missing required field) → adapter raises `ChannelParseError` and the webhook responds HTTP 200 with no scenario action (so BlueBubbles does not retry-storm), and the error is logged at ERROR with `raw_provider_id`.

### REQ-MSG-003 — Vision extraction (image_url → keywords) via LiteLLM proxy [P0]

**WHEN** the scenario advances to the `VISION_PROCESSING` state with a `ChannelMessage` carrying at least one image URL, **THE SYSTEM SHALL** call `app/channels/vision.py::extract(image_url)` which posts a chat-completion request to `LITELLM_BASE_URL` using model `VISION_MODEL` (default `gpt-4o-mini`), and return a structured dict `{"item": str, "color": str, "style": str, "keywords": list[str]}`.

**Acceptance criteria**:
- Successful extraction returns all four fields; `keywords` length ≥ 3 and ≤ 10.
- Vision call timeout = 15 s (httpx); on timeout the scenario falls back to `{"item":"item","color":"","style":"","keywords":[]}` and proceeds, logged at WARN.
- LiteLLM 4xx/5xx → same fallback, logged at ERROR with status code.
- The model prompt is fixed in code (not user-controlled) and instructs the model to respond as JSON only.
- Response parsing is tolerant: if the LLM returns malformed JSON, the system applies the same fallback above (no exception bubbles to the webhook handler).
- The vision call is wrapped in a Langfuse `@observe` span named `channels.vision.extract` (REQ-MSG-009).

### REQ-MSG-004 — 6-step scenario state machine with in-memory session store + 30-min TTL [P0]

**THE SYSTEM SHALL** implement the following six-step state machine per `sender_handle`, persisted in an in-memory `dict` guarded by `asyncio.Lock`, with each session entry expiring 30 minutes after its last update.

| # | State | Entry trigger | Exit action | Next state |
|---|-------|---------------|-------------|------------|
| 1 | `IDLE` | First inbound photo (and optional Pinterest link) | Store `image_url`; persist session | `VISION_PROCESSING` |
| 2 | `VISION_PROCESSING` | Auto on entry | Call `vision.extract`; send opener `"Such a cool {item}! What are you looking for — same vibe, something cheaper, or a specific color?"` | `AWAITING_INTENT` |
| 3 | `AWAITING_INTENT` | Inbound text from same sender | Merge text + vision keywords into `enhanced_query` string | `SEARCHING` |
| 4 | `SEARCHING` | Auto on entry | Invoke `app/pipeline/runner.py` with the enhanced query and the stored image embedding context | `RESULTS_SENT` |
| 5 | `RESULTS_SENT` | Auto on entry | Send 4–5 cards sequentially via `send_card`; then send `"Tap any to see more like it ✨"` | (returns to `IDLE` via TTL or new photo) |
| 6 | `IDLE` (re-entry) | TTL expiry **or** new inbound photo | Drop prior session state; restart from step 1 | `VISION_PROCESSING` |

**Acceptance criteria**:
- A new photo arriving in any state ≠ `VISION_PROCESSING`/`SEARCHING` resets the session and re-enters step 1.
- A text message arriving in `IDLE` (no prior photo) → bot replies `"Send me a photo first 📸"` and stays in `IDLE`. Session is NOT created.
- A text message arriving in `VISION_PROCESSING` or `SEARCHING` → buffered (appended to a pending text list on the session) and consumed when the state machine reaches `AWAITING_INTENT`.
- TTL = 30 minutes, measured from the session's `updated_at` timestamp; checked lazily on access (no background sweeper required for the demo).
- All session reads/writes go through `asyncio.Lock` per `sender_handle` to prevent interleaving when two webhook events arrive within the same event-loop tick.
- The bot opener text and closer text are constants in code; both MUST be English (REQ on `BOT_LANGUAGE` default `en`).
- The `{item}` placeholder in the opener is filled from `vision.extract().item`; if empty, the literal string `"piece"` is used.
- Single-worker assumption is documented inline in `app/channels/session.py` module docstring; multi-worker support is explicitly out of scope.

### REQ-MSG-005 — BlueBubbles outbound send_text + send_card (image attachment) [P0]

**WHEN** the scenario calls `MessengerAdapter.send_text(to, text)` or `MessengerAdapter.send_card(to, image_url, title, subtitle, link)`, **THE SYSTEM SHALL** translate the call into one or more BlueBubbles REST requests against `BLUEBUBBLES_SERVER_URL` authenticated with `BLUEBUBBLES_PASSWORD`.

**Acceptance criteria**:
- `send_text` posts a single text message to the addressed handle.
- `send_card` sends the product image as an attachment, followed by a single text message containing `title`, `subtitle`, and `link` on separate lines (since iMessage has no native rich card primitive).
- Cards within a single recommendation set are sent **sequentially** (not parallel) to preserve order in the iMessage thread; ordering MUST match the pipeline's returned product list.
- BlueBubbles REST timeout = 20 s per call; on failure the adapter retries once with a 1 s backoff, then logs ERROR and aborts the remaining cards in that batch.
- `send_card` truncates `title` to 80 chars and `subtitle` to 120 chars.
- All outbound calls are logged at INFO with `to`, `kind` (`text`|`card`), and elapsed ms; image URLs are logged but request bodies are not (PII safety).
- The adapter MUST NOT log `BLUEBUBBLES_PASSWORD` or HMAC secrets in any code path.

### REQ-MSG-006 — Pipeline integration: enhanced query → existing runner → top 4–5 cards [P0]

**WHEN** the scenario reaches `SEARCHING`, **THE SYSTEM SHALL** invoke `app/pipeline/runner.py` (existing) with the stored image URL and the merged `enhanced_query`, and select the top 4–5 results from the pipeline's existing top-15 output for delivery as cards.

**Acceptance criteria**:
- The pipeline call reuses the existing `PipelineState` contract — no new fields added to the runner signature.
- The merged query is constructed as: `f"{user_text} {' '.join(vision.keywords)}"` (whitespace-joined, lowercased, trimmed to 256 chars).
- If the pipeline returns fewer than 4 results, the adapter sends whatever is available (minimum 1) and still sends the closing line.
- If the pipeline returns 0 results, the bot replies `"Hmm, I couldn't find a match — try another angle or a different photo."` and the session transitions back to `IDLE`.
- Each card is built from the pipeline result fields: `image_url`, `brand`, `price` (formatted with currency symbol from result), `product_url`. The `title` is `brand`, the `subtitle` is `price`, the `link` is `product_url`.
- Pipeline call latency budget: end-to-end (vision + pipeline + outbound) target < 12 s P95 for the demo. Budget is informative, not a hard fail; SLOs are tracked in Langfuse.
- The pipeline call is wrapped in a Langfuse span as a child of the per-session trace (REQ-MSG-009).

### REQ-MSG-007 — MessengerAdapter ABC + backend toggle via MESSENGER_BACKEND env [P0]

**THE SYSTEM SHALL** define `MessengerAdapter` as an abstract base class in `app/channels/adapter.py` with the following abstract methods, and **WHEN** the FastAPI app starts, **THE SYSTEM SHALL** instantiate exactly one concrete adapter selected by the `MESSENGER_BACKEND` environment variable (default `bluebubbles`).

```
parse_inbound(payload: dict) -> ChannelMessage
send_text(to: str, text: str) -> None
send_card(to: str, image_url: str, title: str, subtitle: str, link: str) -> None
```

**Acceptance criteria**:
- `MESSENGER_BACKEND=bluebubbles` (default) → `BlueBubblesAdapter` is wired.
- `MESSENGER_BACKEND=telegram` → `TelegramAdapter` is wired (skeleton; raises `NotImplementedError` on send paths in P0 — see REQ-MSG-008).
- `MESSENGER_BACKEND=sendblue` → app startup fails fast with a clear error message: `"sendblue backend is not implemented; deferred fallback only"`.
- Unknown value → app startup fails fast with `ValueError` listing accepted values.
- The selected adapter instance is exposed to FastAPI handlers via a dependency (`get_messenger_adapter`) for testability.
- Adding a new backend requires only: (a) implementing the ABC, (b) registering in the `MESSENGER_BACKEND` switch — no changes to `scenario.py` or webhook routing.

### REQ-MSG-008 — Telegram adapter stub (skeleton only, post-demo implementation) [P2]

**WHILE** Telegram is not the demo backend, **THE SYSTEM SHALL** ship a `TelegramAdapter` class that satisfies the `MessengerAdapter` ABC interface but raises `NotImplementedError("telegram adapter scheduled for post-demo")` from `parse_inbound`, `send_text`, and `send_card`.

**Acceptance criteria**:
- The class exists at `app/channels/telegram/adapter.py` and inherits from `MessengerAdapter`.
- A module-level docstring states "P2 — post-demo implementation tracked under SPEC-MSG-001 REQ-MSG-008".
- No Telegram webhook route is registered in P0 (no `/webhooks/telegram`).
- No Telegram-specific environment variables are required at startup; the stub never reads any env at import time.
- This requirement is **non-blocking** for the 2026-05-07 demo. P0 ship criteria do not check this REQ.

### REQ-MSG-009 — Observability: Langfuse @observe wraps the full inbound→reply trace as one session [P0]

**WHEN** a verified webhook is processed, **THE SYSTEM SHALL** open a single Langfuse trace (via the existing `app/observability/langfuse.py @observe` wrapper) whose `session_id = sender_handle`, with child spans for `channels.parse_inbound`, `channels.vision.extract`, `pipeline.runner`, and `channels.send_batch`.

**Acceptance criteria**:
- Trace `session_id` equals the sender's iMessage handle (phone number or Apple ID), so multiple turns from the same user are grouped in the Langfuse UI.
- Trace `name` = `channels.imessage.turn`.
- Each child span carries metadata: `state_before`, `state_after`, and (for vision/pipeline) elapsed ms.
- If `LANGFUSE_*` env vars are unset, the existing no-op fallback in `app/observability/langfuse.py` applies — no errors raised, no behavior change.
- PII safety: the inbound `text` may be logged into Langfuse input; the vision `image_url` MAY be logged; sender phone numbers MUST be hashed (SHA-256, first 16 hex chars) before being used as `session_id` to avoid storing raw PII in Langfuse.

### REQ-MSG-010 — Health endpoint extension: /health/ready reports messenger backend status [P0]

**WHEN** `GET /health/ready` is called (existing endpoint, requires `X-Internal-Token`), **THE SYSTEM SHALL** include in the JSON response a `messenger` block containing: `{"backend": "<MESSENGER_BACKEND>", "configured": <bool>, "outbound_reachable": <bool>}`.

**Acceptance criteria**:
- `configured = True` iff all required env vars for the selected backend are present and non-empty (BlueBubbles: `BLUEBUBBLES_SERVER_URL`, `BLUEBUBBLES_PASSWORD`, `BLUEBUBBLES_WEBHOOK_SECRET`).
- `outbound_reachable = True` iff a `GET {BLUEBUBBLES_SERVER_URL}/api/v1/server/info` (or equivalent ping) returns 2xx within 3 s; `False` on timeout/error. The check MUST NOT block the rest of `/health/ready` longer than 3 s.
- The `/health` (liveness) endpoint is unchanged and does NOT call BlueBubbles.
- For `MESSENGER_BACKEND=telegram` (P2 stub), `configured = False` and `outbound_reachable = False`, with no exception raised.

---

## Environment Variables (introduced by this SPEC)

| Var | Required | Default | Description |
|-----|----------|---------|-------------|
| `MESSENGER_BACKEND` | no | `bluebubbles` | Adapter selector. Accepted: `bluebubbles`, `telegram`, `sendblue` (sendblue startup-fails). |
| `BLUEBUBBLES_SERVER_URL` | yes (when backend=bluebubbles) | — | BlueBubbles REST base URL exposed via Cloudflare Tunnel (e.g. `https://bb.example.com`). |
| `BLUEBUBBLES_PASSWORD` | yes (when backend=bluebubbles) | — | BlueBubbles server password (used in REST `password` query param or auth header per BB API). |
| `BLUEBUBBLES_WEBHOOK_SECRET` | yes (when backend=bluebubbles) | — | Shared secret for HMAC-SHA256 signature on inbound webhook (REQ-MSG-001). |
| `VISION_MODEL` | no | `gpt-4o-mini` | Model id passed to LiteLLM proxy for vision extraction. |
| `BOT_LANGUAGE` | no | `en` | Bot reply language. P0 SHALL only support `en`. |

All values live in `.env` for the demo (POC stance per project README); production migration to Parameter Store is out of scope for this SPEC.

---

## Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|------------|
| R1 | **Apple ID ban / rate-limiting.** Apple actively detects automation on iMessage. A banned ID kills the demo channel. | Medium | High | Use a **dedicated, fresh Apple ID** provisioned only for the demo; no personal phone number. Keep daily outbound volume low (single-digit conversations). Have a backup Apple ID pre-registered. |
| R2 | **Mac sleep / reboot kills the bot.** BlueBubbles requires Messages.app running on a logged-in macOS session. Auto-sleep, OS update reboot, or power loss takes the bot offline silently. | High | High | Disable sleep (`caffeinate -d` or Energy Saver settings). Disable auto-update during demo week. Configure auto-login. Add `/health/ready` `outbound_reachable` flag (REQ-MSG-010) to surface outage. Owner monitors before each demo recording session. |
| R3 | **Cloudflare Tunnel single point of failure.** If the tunnel drops, inbound webhooks vanish silently — BlueBubbles does not buffer to disk. | Medium | High | Run `cloudflared` as a `launchd` service with auto-restart. Pre-test the public URL minutes before demo. Document a fallback: switch `MESSENGER_BACKEND` env and restart AI server (no code change). |
| R4 | **Single-worker, in-memory session store.** Any restart of the AI server wipes all in-flight conversations. Two uvicorn workers would split sessions across processes and break the state machine. | High (restarts) | Medium | Run uvicorn with `--workers 1` for the demo. Document inline in `session.py`. Post-demo plan: migrate to Redis (out of scope here). |
| R5 | **Vision model returns junk / wrong language.** GPT-4o-mini may misclassify or respond in Korean for a Korean caption, breaking the bot opener. | Medium | Medium | Hard-code English-only system prompt. Tolerate malformed JSON (REQ-MSG-003 fallback). Manually rehearse with the exact demo photo before recording. |
| R6 | **HMAC secret leaks via logs or repo.** | Low | High | Constant-time compare (REQ-MSG-001). Explicit "do not log secrets" rule (REQ-MSG-005 acceptance). Pre-commit secret scan (existing project setup). |
| R7 | **Pipeline latency > 12 s blows the demo pacing.** | Medium | Medium | Pipeline reuse means latency is bounded by existing v5 search behavior (already tuned). Pre-warm Modal `/embed` before recording. Track P95 in Langfuse (REQ-MSG-009). |
| R8 | **Image URL SSRF.** A malicious sender could craft an attachment URL pointing to internal infra. | Low (closed channel) | High | Reuse the SSRF guard from `app/models/request.py` (REQ-MSG-002). |
| R9 | **Demo deadline slip (2026-05-07).** Only ~3 days from this SPEC to the recording. | Medium | High | P0 scope is intentionally minimal (no Telegram, no persistence, no rich cards). REQ-MSG-008 is explicitly non-blocking. Daily check-in against this REQ list. |
| R10 | **PII in observability.** Sender phone numbers in Langfuse violate privacy expectations. | Medium | Medium | Hash sender handle before use as `session_id` (REQ-MSG-009 acceptance). |

---

## Exclusions (What NOT to Build)

The following are explicitly out of scope for SPEC-MSG-001 and MUST NOT be implemented as part of this SPEC:

1. **Persistent session storage** (Redis / Postgres / SQLite). In-memory only. Deferred to a post-demo SPEC.
2. **Multi-worker uvicorn deployment.** Single worker only.
3. **Telegram production code** (webhook handler, real `send_text`/`send_card` bodies). Skeleton stub only per REQ-MSG-008.
4. **Sendblue / LoopMessage SaaS adapters.** `MESSENGER_BACKEND=sendblue` startup-fails by design.
5. **iMessage group chats, reactions, tapbacks, typing indicators, read receipts.**
6. **End-user authentication or Supabase Auth account linking.** Sender handle is the only identity.
7. **Outbound rate limiting / anti-spam / per-user quotas.**
8. **Localized bot copy.** English only (`BOT_LANGUAGE=en`); other languages are a post-demo concern.
9. **Modifications to `app/pipeline/runner.py`, `app/providers/`, or `app/api/recommend.py`.** Reuse only.
10. **Web UI / admin console for monitoring conversations.** Langfuse UI is sufficient.
11. **Automatic Mac watchdog / auto-reboot tooling.** Operator-monitored manually for the demo.
12. **Telegram environment variable validation.** REQ-MSG-008 stub MUST NOT require Telegram env at startup.

---

## Open Questions (to resolve during plan.md / implementation)

These do not block SPEC approval but should be answered before code is written:

1. Does BlueBubbles deliver image attachments as durable HTTPS URLs, or as time-limited proxy URLs that must be downloaded immediately? (Affects whether `image_url` can be passed straight to Modal `/embed` or must be re-uploaded to R2 first. The Vercel `portal/app` already does R2 upload — we may need to mirror that.)
2. What is the exact BlueBubbles HMAC scheme — header name, encoding (hex vs base64), and the body fragment that is signed? (REQ-MSG-001 assumes `X-BB-Signature` + `HMAC-SHA256(secret, raw_body)` hex; verify against BB docs.)
3. Pipeline returns top-15 — should the bot send strictly the top 4 ordered, or apply a small re-rank for visual diversity in the chat? P0 default: top-4 ordered (no re-rank).

---

## Cross-References

- Existing pipeline contract: `app/pipeline/runner.py`, `app/pipeline/state.py` (reused unchanged).
- LiteLLM proxy contract: `app/providers/llm.py` (vision call follows same `LITELLM_BASE_URL` pattern).
- Auth / health pattern: `app/core/auth.py`, `app/api/health.py` (REQ-MSG-010 extends `/health/ready`).
- Observability pattern: `app/observability/langfuse.py` (REQ-MSG-009 reuses `@observe`).
- Project context: `/Users/hansangho/Desktop/portal/ai/CLAUDE.md` — AI server is stateless and reuses LiteLLM + Modal + Supabase.
- Infra ownership: `/Users/hansangho/Desktop/aws-infra/portal-ai-servers/portal-ai/` (BlueBubbles host config and Cloudflare Tunnel are tracked there, not here).

---

## Definition of Done (P0, demo-blocking)

- [ ] REQ-MSG-001 through REQ-MSG-007, REQ-MSG-009, REQ-MSG-010 implemented and acceptance criteria verified.
- [ ] REQ-MSG-008 (Telegram stub) class-shell present; full implementation NOT required.
- [ ] End-to-end manual test: photo + text → 4 cards back, on a real iMessage device, recorded latency < 15 s for the recorded turn.
- [ ] `/health/ready` returns `messenger.outbound_reachable=true` immediately before demo recording.
- [ ] Langfuse trace for the recorded turn is visible with all four child spans.
- [ ] No secrets in committed code (`.env.example` updated with placeholder vars).
- [ ] `ruff check . && ruff format --check .` passes.
