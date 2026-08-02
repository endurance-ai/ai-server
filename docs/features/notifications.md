# Native push notifications

The first release sends native iOS notifications directly through APNs. Android
tokens can be registered for forward compatibility, but FCM delivery is not part
of this release.

## Product policy

| Category | Detection | Scan | Delivery | Limits |
| --- | --- | --- | --- | --- |
| Saved-product digest | Restock and price drop for saved products | 09:00 KST | 09:30 KST | One digest per user/day |
| Followed-brand digest | Recent products from selected brands | 10:30 KST | 11:00 KST | Up to 5 items; max 3 accepted days in a rolling 7-day window |

Quiet hours are 21:00–09:00 KST. A detector delayed into the active window
catches up immediately; work found during quiet hours is moved to the next
category delivery time. Undelivered brand overflow remains eligible during the
14-day candidate window.

Consent is evaluated server-side. `system=false` disables every notification;
brand-new notifications additionally require `release_alerts=true`. The hidden
`restock`, `price_drop`, and `brand_new_product` keys are supported for a future
mobile UI and default to true.

## Architecture and invariants

```text
products/saves/brand picks
        │ detect (separate worker; advisory lock)
        ▼
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
tables, while `kiko.ai-app/database/migrations/101_notification_candidate_indexes.sql`
owns the public product candidate index.

## Apple Developer setup

The production topic is the exact iOS bundle identifier: `com.kikoai.app`.

1. Sign in to Apple Developer with an Account Holder or Admin role. Open
   **Certificates, Identifiers & Profiles → Identifiers**, select the explicit
   App ID for `com.kikoai.app`, enable **Push Notifications**, and save.
2. Open **Keys**, create a key such as `kiko APNs provider`, enable **Apple Push
   Notifications service (APNs)**, register it, and download `AuthKey_<KEY_ID>.p8`.
   Apple permits this download once; put it in a secrets manager immediately.
   Do not commit it or copy it into the app bundle.
3. Record the 10-character Key ID shown for the key and the 10-character Team ID
   from Membership details. One APNs signing key works for both development and
   production and can serve multiple apps in the same team.
4. Regenerate the iOS provisioning profiles/build after enabling the capability.
   The signed development app must contain `aps-environment=development`; an
   App Store/TestFlight archive uses `production`. Verify the built entitlement,
   not an assumed build-name mapping.
5. Configure the worker secret values. The same `.p8` and Key ID may be assigned
   to both environment-specific variables:

   ```dotenv
   APNS_TOPIC=com.kikoai.app
   APNS_TEAM_ID=<APPLE_TEAM_ID>
   APNS_SANDBOX_KEY_ID=<APNS_KEY_ID>
   APNS_SANDBOX_AUTH_KEY_B64=<BASE64_OF_P8>
   APNS_PRODUCTION_KEY_ID=<APNS_KEY_ID>
   APNS_PRODUCTION_AUTH_KEY_B64=<BASE64_OF_P8>
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
```

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
