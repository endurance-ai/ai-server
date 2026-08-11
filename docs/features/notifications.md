# Native push notifications

The first release sends native iOS notifications directly through APNs. Android
tokens can be registered for forward compatibility, but FCM delivery is not part
of this release.

## Product policy

| Category | Detection | Scan | Delivery | Limits |
| --- | --- | --- | --- | --- |
| Saved-product digest | Restock and price drop for saved products | 09:00 KST | 09:30 KST | One digest per user/day |
| Followed-brand digest | Recent products from selected brands | 10:30 KST | 11:00 KST | Up to 5 items; max 3 accepted days in a rolling 7-day window |
| Brand-sale digest | A brand's discounted-catalog ratio crosses 30% | 10:30 KST | 11:00 KST | Max 3 accepted days in a rolling 7-day window |

Quiet hours are 22:00–08:00 KST. A detector delayed into the active window
catches up immediately; work found during quiet hours is moved to the next
category delivery time. Undelivered brand overflow remains eligible during the
14-day candidate window.

Consent is evaluated server-side. `system=false` disables every notification;
brand-new notifications additionally require `release_alerts=true`. The hidden
`restock`, `price_drop`, and `brand_new_product` keys are supported for a future
mobile UI and default to true. `brand_sale` has no dedicated consent key — it
shares `brand_new_product` because the mobile UI groups both under one "brand
notifications" toggle.

**Consent gates the push, not the inbox.** Turning a category off must not erase
the news itself. `restock` and `price_drop` are still recorded in
`ai.notifications` (marked `suppressed_reason = 'consent_off'`, no message or
delivery), and `brand_sale` is served from the canon. This matters most for saved
products: `detect_save_events` advances the baseline the moment it fires, so an
event dropped at this point is gone for good — re-enabling consent later cannot
recover it.

Only **consent** failures are recorded this way. Weekly-cap and daily-overflow
misses stay unrecorded on purpose: `_NEW_PRODUCT_SQL` picks candidates by
anti-joining `ai.notifications`, so recording them would drop them from the retry
pool. Consent-off is a permanent suppression, so there is no retry to preserve.
Suppressed `brand_new_product` rows are still capped at
`NOTIFY_BRAND_NEW_MAX_ITEMS` per run, or a user with the category off would get
the whole 14-day candidate window dumped into their inbox at once.

## Brand news is canonical, not fanned out

`brand_sale` is the one category whose content is a **brand-level fact**, so it
is stored once in `ai.brand_news` (migration `0027`) and read by followers rather
than copied to them. Consequences the other categories do not share:

- Detection scans **every** brand, not just followed ones. A brand nobody follows
  still gets its news row, because the brand home page (`GET /v1/brands/{id}`) is
  public and reads `ai.brand_news` directly. Scanning all brands makes catalog
  size a noise source, so `NOTIFY_BRAND_SALE_MIN_PRODUCTS` (default 10) excludes
  brands too small for a ratio to mean anything.
- The inbox reads the canon, so consent and the weekly cap gate **only the APNs
  push**. A user who turned push off still sees the news in their inbox.
- `ai.notifications` rows for `brand_sale` are written only for users who pass
  the push gate. Their sole job is anchoring the outbox
  (`notification_message_events.notification_id` is a foreign key), so the feed
  excludes `kind = 'brand_sale'` to avoid showing the same news twice.
  Suppressed pushes are counted in the batch report, not stored as rows.

Deduplication comes from the `ai.brand_sale_state` false→true transition plus the
`uq_brand_news_open` partial unique index, not from an anti-join against
`ai.notifications` — which is what makes the above safe.

`ai.brand_sale_state` holds the current answer to "is this brand on sale"
(upserted, no history). `ai.brand_news` holds the history via
`started_at`/`ended_at`. A sale ending closes the open row rather than deleting
it.

### `products.created_at` is an ingest time, not a release date

Both new-arrival paths have to work around this. Crawls run per brand and bulk
insert, so `created_at` records when a row first landed in our database — one
measured brand wrote 775 rows inside two seconds. Two consequences, each with its
own guard:

- **A brand's first crawl is not a wave of new arrivals.** Onboarding a brand
  imports its whole back catalogue at once; 13% of a measured 14-day candidate
  set was exactly this. Rows within `NOTIFY_BRAND_ONBOARDING_GRACE_H` (24h) of a
  brand's earliest product are excluded from both `_NEW_PRODUCT_SQL` and
  `_BRAND_NEW_SUMMARY_SQL`.
- **"Newest first" really means "crawled last".** Taking the top
  `NOTIFY_BRAND_NEW_MAX_ITEMS` would hand the whole day to whichever brand the
  crawler visited most recently, burying every other followed brand.
  `pick_brand_new` caps each brand at `NOTIFY_BRAND_NEW_MAX_PER_BRAND` (2) first,
  then backfills any unused slots from the overflow so that a user following a
  single brand still receives a full day.

Both guards apply to the push path and the consent-suppressed inbox path
identically — otherwise the inbox would reproduce the skew the push path avoids.

### Two news kinds, two audiences

`ai.brand_news.kind` (migration `0029`):

| kind | Trigger | Brand home | Inbox |
| --- | --- | --- | --- |
| `brand_sale` | discounted-catalog ratio crosses the threshold (state transition) | yes | yes |
| `brand_new` | rolling count of arrivals in `NOTIFY_BRAND_NEW_SUMMARY_WINDOW_D` days | yes | **no** |

Brand home serves the newest `_NEWS_LIMIT` (5) inline on
`GET /v1/brands/{id}`; `GET /v1/brands/{id}/news` pages past that preview with the
same ordering and copy. Both are public — news is a brand-level fact, independent
of who is looking.

`brand_new` exists because sales are rare, so a brand home that only surfaces
sales looks empty most of the time. It stays out of the inbox because the inbox
already receives per-product `brand_new_product` rows matched to the user's
gender — a brand-level summary on top would say the same thing twice. The brand
home has no user to personalise for (it is public), so there the summary is the
only sensible form. `app/api/notifications.py` `_INBOX_NEWS_KINDS` enforces this.

Unlike a sale, the summary is not an on/off transition but a rolling aggregate:
one open row per brand whose `payload.new_count` is refreshed each run, closed
when the window empties. A refresh is not "news", so it neither reopens the row
nor moves `started_at`.

## Inbox feed: two sources, two read models

`GET /v1/notifications` merges two sources (migration `0028`):

| Source | Table | Fan-out | Read state |
| --- | --- | --- | --- |
| `n` | `ai.notifications` (restock, price_drop, brand_new_product) | write — rows already differ per user | `read_at` on the row |
| `b` | `ai.brand_news` joined to `ai.user_brand_picks` | read — one shared row per brand | `ai.user_feed_state` watermark + `ai.feed_reads` exceptions |

Item ids are `<source>:<row id>` (`"n:123"`, `"b:45"`) because the two id spaces
overlap; `PATCH /v1/notifications/read` takes the same strings back. The cursor is
an opaque base64 token over `(created_at, source, id)`, all three descending —
mixing sort directions would break the row-wise comparison that drives keyset
pagination.

Read state is deliberately **not** unified. Per-user rows can carry a per-row
`read_at`; a shared row cannot, so it needs the watermark. Marking everything read
becomes a single-row update on `ai.user_feed_state` and prunes `ai.feed_reads`,
since the watermark then covers every exception.

Brand news is scoped to `started_at >= user_brand_picks.created_at`, so following
a brand does not retroactively fill the inbox with its past sales.

## Architecture and invariants

```text
products/saves/brand picks
        │ detect (separate worker; advisory lock)
        ▼
ai.brand_news ─────┐  brand-level canonical news (brand_sale only)
 brand home reads  │  O(brands × events) — independent of user count
   this directly   │
                   ▼ fan out to notify_enabled followers
ai.notifications ── ai.notification_messages ── ai.notification_deliveries
 domain event             user digest                  device attempt
                                                        │
                                                        ▼ HTTP/2 + JWT
                                              APNs sandbox / production
```

- Detection, baseline advancement, event recording, digest creation, and
  per-device delivery creation commit in one PostgreSQL transaction.
- The web process does not schedule or send pushes. Run the worker as a separate
  process; PostgreSQL advisory locks make detector scheduling singleton-safe and
  `FOR UPDATE SKIP LOCKED` permits multiple delivery workers.
- Saved-product events deduplicate per KST day. Brand/product events deduplicate
  for the lifetime of the pair. Messages deduplicate per user/category/day.
- A push endpoint is globally unique by provider, APNs environment, topic, and
  token. Registering it after account switching transfers ownership to the
  currently authenticated user.
- Deliveries preserve a stable `apns-id`, use bounded concurrency, expire at
  quiet hours, and retry transient errors with jittered exponential backoff.
  `Unregistered`/`ExpiredToken` endpoints are disabled only when APNs' timestamp
  is not older than the latest registration. Repeated `BadDeviceToken` responses
  quarantine the endpoint.
- APNs payloads are intentionally small: schema version, message id, category,
  primary product id, item count, and route. Product collections stay in the DB.
  Device tokens are fingerprinted in logs, never logged directly.

Schema ownership is split deliberately: Alembic `0023`/`0024` owns `ai.*` outbox
tables, `0025`–`0028` add the inbox feed, brand follow, brand-news canon, and feed
read state, while `kiko.ai-app/database/migrations/101_notification_candidate_indexes.sql`
owns the public product candidate index.

## Apple Developer setup

The production topic is the exact iOS bundle identifier: `com.kikoai.app`.

1. Sign in to Apple Developer with an Account Holder or Admin role. Open
   **Certificates, Identifiers & Profiles → Identifiers**, select the explicit
   App ID for `com.kikoai.app`, enable **Push Notifications**, and save.
2. Open **Keys** and create two topic-specific APNs keys for `com.kikoai.app`:
   one restricted to Sandbox and one restricted to Production. Download each
   `AuthKey_<KEY_ID>.p8`. Apple permits each download once; put both in a secrets
   manager immediately. Do not commit them or copy them into the app bundle.
   Legacy APNs keys created before environment scoping may still support both
   environments, but new deployments should keep the credentials separate.
3. Record both 10-character Key IDs and the 10-character Team ID from Membership
   details.
4. Regenerate the iOS provisioning profiles/build after enabling the capability.
   The signed development app must contain `aps-environment=development`; an
   App Store/TestFlight archive uses `production`. Verify the built entitlement,
   not an assumed build-name mapping.
5. Configure the worker with the corresponding environment-specific secrets:

   ```dotenv
   APNS_TOPIC=com.kikoai.app
   APNS_TEAM_ID=<APPLE_TEAM_ID>
   APNS_SANDBOX_KEY_ID=<SANDBOX_APNS_KEY_ID>
   APNS_SANDBOX_AUTH_KEY_B64=<BASE64_OF_SANDBOX_P8>
   APNS_PRODUCTION_KEY_ID=<PRODUCTION_APNS_KEY_ID>
   APNS_PRODUCTION_AUTH_KEY_B64=<BASE64_OF_PRODUCTION_P8>
   NOTIFICATION_WORKER_ENABLED=true
   ```

   `*_AUTH_KEY_PATH` is available for a mounted secret file instead of base64.
   Prefer a secrets-manager/mounted-file value in production. The legacy
   `APNS_AUTH_KEY`, `APNS_AUTH_KEY_PATH`, and `APNS_KEY_ID` remain only as a
   production fallback during migration.
6. Permit outbound TCP 443 from the worker to
   `api.sandbox.push.apple.com` and `api.push.apple.com`. No inbound APNs port is
   required. Keep system time synchronized because provider JWTs are time-bound.

Apple references: [register the app and token with APNs](https://developer.apple.com/documentation/usernotifications/registering-your-app-with-apns),
[create token authentication credentials](https://developer.apple.com/help/account/capabilities/communicate-with-apns-using-authentication-tokens), and
[send provider requests](https://developer.apple.com/documentation/usernotifications/sending-notification-requests-to-apns).

## Database and worker rollout

Do not enable sending before both migrations are applied.

1. Back up the shared database and apply `kiko.ai-app` migration 101 through its
   normal migration workflow.
2. Apply the ai-server Alembic head. Migration 0024 first resolves legacy token
   duplicates by keeping the most recently registered owner, then installs the
   global endpoint key and transactional outbox.
3. Deploy one standalone process from the same image:

   ```text
   python -m app.workers.notification_worker
   ```

   It needs `DB_DSN`, APNs secrets, and `NOTIFICATION_WORKER_ENABLED=true`; it
   does not need an HTTP port. For local compose only:

   ```bash
   docker compose --profile notifications up notification-worker
   ```

4. First run detection without writes:

   ```bash
   python -m app.workers.notification_worker --dry-run
   ```

   Then validate one authenticated test user with
   `scripts/notify_batch.py --only-user=<uuid> --limit=1`. This creates a durable
   outbox item; the worker sends it at the policy time. Test a development build
   against sandbox first, then TestFlight against production.
5. Roll out with one worker initially. Multiple replicas are safe, but only add
   them after the due-delivery backlog demonstrates a need.

## Operations

Health and backlog queries (read-only):

```sql
SELECT job, last_succeeded_at, heartbeat_at, last_error
FROM ai.notification_job_state ORDER BY job;

SELECT status, count(*) FROM ai.notification_messages GROUP BY status;
SELECT status, count(*) FROM ai.notification_deliveries GROUP BY status;

SELECT count(*) AS overdue
FROM ai.notification_deliveries d
JOIN ai.notification_messages m USING (message_id)
WHERE d.status IN ('pending', 'retry', 'processing')
  AND d.next_attempt_at <= now() AND m.expires_at > now();

-- Brand news currently shown on brand home pages.
SELECT count(*) FILTER (WHERE ended_at IS NULL) AS open,
       count(*)                                 AS total
FROM ai.brand_news;

-- Feed read-state size. feed_reads should stay small: "mark all read" prunes it.
SELECT (SELECT count(*) FROM ai.user_feed_state) AS watermarks,
       (SELECT count(*) FROM ai.feed_reads)      AS exceptions;
```

An open brand-news row that never closes means the brand has stayed above the
sale threshold, which is legitimate for a long promotion but worth checking
against `ai.brand_sale_state.updated_at` if it persists for weeks.

Alert when a detector has no successful run for 26 hours, `last_error` is set,
overdue deliveries grow for two poll intervals, or APNs configuration failures
appear. The immediate kill switch is `NOTIFICATION_WORKER_ENABLED=false` plus a
worker restart; queued records remain durable. Revoking the APNs key is reserved
for suspected credential compromise—rotate first, verify, then revoke the old
key.

## Mobile-team handoff (no mobile changes are included here)

The current app already obtains a native token, but the following work is needed
before enabling the worker:

1. Add the SDK 56 `expo-notifications` config plugin to `app.json`, regenerate
   native projects/provisioning, and make a new native build. Remote push cannot
   be enabled by an OTA JavaScript update alone.
2. On every authenticated launch and through `addPushTokenListener`, send the
   current native token to `POST /v1/devices` using:

   ```json
   {
     "device_id": "<previously returned id, omit on first registration>",
     "push_token": "<native APNs token>",
     "platform": "ios",
     "provider": "apns",
     "environment": "development | production",
     "app_version": "...",
     "device_model": "..."
   }
   ```

   Derive the APNs environment from the signed app/runtime API, not `__DEV__`.
   The legacy `apns_token` request remains accepted temporarily.
3. Persist the returned `device_id` per installation. Include it in logout as
   `{ "refresh_token": "...", "device_id": "..." }`; local logout should still
   finish if the network request fails.
4. Use the iOS-specific authorization status so provisional authorization is
   handled intentionally. Retry token registration after transient failures and
   never treat a cached token as permanent.
5. Install a notification response listener and route the versioned payload:
   `/product/:id`, `/wishlist`, or `/home`. Ignore unknown schema versions/routes
   safely. Also define foreground presentation behavior with a notification
   handler.
6. Verify on physical devices: fresh permission grant, denial, provisional
   state, foreground/background/terminated tap routing, account switch on one
   device, logout, token rotation, sandbox development build, and production
   TestFlight build.

Expo SDK 56 reference: [expo-notifications](https://docs.expo.dev/versions/v56.0.0/sdk/notifications/).
