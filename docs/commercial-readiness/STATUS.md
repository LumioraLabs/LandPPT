# Commercial Readiness Status

Last updated: 2026-09-26 (America/Toronto)

## Executive status

Current verdict: **not ready for a mature paid public launch**.

Phase 1 of the implementation plan is now in the worktree: the full automated suite is green, production mode refuses insecure settings before migrations, SQLite and PostgreSQL migration rehearsals pass, and the production Helm chart fails closed until the public URL, hosts, proxy CIDRs, billing flags, and existing-secret key names are supplied. Payment recovery, usage reservations, legal/privacy workflows, observability ownership, backups, and live-environment validation are still open.

No implementation in this worktree has been committed.

## Implemented

### Authentication and browser security

- [x] Argon2id password hashing through `argon2-cffi`.
- [x] Compatibility verification and transparent migration for legacy SHA-256 password hashes.
- [x] Password hashes removed from session-cache payloads.
- [x] Password changes and resets revoke existing database sessions.
- [x] Centralized secure session-cookie policy.
- [x] Per-account and per-IP login rate limiting.
- [x] Forwarded headers trusted only from configured proxy CIDRs.
- [x] OAuth redirect targets restricted to safe local paths.
- [x] Request base URLs no longer trust arbitrary Origin or Referer values.
- [x] Explicit CORS allowlist support and finite methods/headers.
- [x] Optional trusted-host enforcement.
- [x] Same-origin protection for cookie-authenticated state-changing browser requests.
- [x] Baseline browser security headers and production HSTS behavior.
- [x] GitHub and LinuxDo session-cookie handling uses the centralized policy.

Primary files:

- `src/landppt/auth/passwords.py`
- `src/landppt/auth/cookies.py`
- `src/landppt/auth/security.py`
- `src/landppt/auth/middleware.py`
- `src/landppt/auth/routes.py`
- `src/landppt/auth/request_context.py`

### SSRF and remote-content resource controls

- [x] Remote fetches accept only HTTP and HTTPS URLs.
- [x] URL credentials, local/internal hostnames, and non-global IP addresses are rejected.
- [x] DNS results are validated when connections are created.
- [x] Every redirect target is validated.
- [x] Remote response bodies and URL-source files have strict size limits.
- [x] Web extraction, URL outline generation, deep research, and the model curl tool use the safe fetch path.

Primary file: `src/landppt/utils/safe_fetch.py`.

### Untrusted generated HTML isolation

- [x] Slide, template, preview, export, fullscreen, share, and editor iframe paths are sandboxed.
- [x] The sandbox deliberately omits `allow-scripts`, forms, popups, and top-level navigation.
- [x] Referrer data is suppressed for these frames.

The implementation retains `allow-same-origin` where the parent editor/exporter still needs DOM access. This is acceptable only while scripts remain disallowed and must be re-reviewed if frame capabilities change.

### Configuration secrets and email verification

- [x] Password-typed database settings are encrypted with Fernet/MultiFernet.
- [x] Key rotation is supported through `LANDPPT_CONFIG_ENCRYPTION_KEYS`.
- [x] Provider, OAuth, SMTP, and Resend secrets are redacted from admin responses.
- [x] Blank secret fields preserve existing credentials.
- [x] Email delivery reloads encrypted database settings after process restarts.
- [x] The admin `.env` editor was removed.
- [x] Database configuration APIs no longer write runtime settings into `.env`.
- [x] Migration `020` encrypts legacy plaintext password-typed settings.
- [x] Verification codes use cryptographically secure randomness.
- [x] Verification codes are stored as keyed, context-bound hashes rather than plaintext.
- [x] Verification attempts are limited.
- [x] Migration `021` expands and migrates verification-code storage.

Primary files:

- `src/landppt/core/secret_storage.py`
- `src/landppt/services/db_config_service.py`
- `src/landppt/services/email_service.py`
- `src/landppt/web/admin_routes.py`

### Atomic credit ledger

- [x] Credit consumption uses a conditional atomic SQL update.
- [x] Ledger writes occur in the same transaction as balance changes.
- [x] Idempotency keys protect retryable additions and consumptions.
- [x] Concurrent redemption attempts can produce only one winner.
- [x] Migration `019` adds unique credit-transaction idempotency keys.
- [x] New users retain configured signup credits regardless of temporary enforcement mode.

Primary file: `src/landppt/services/credits_service.py`.

### Initial Stripe payment implementation

- [x] Official Stripe Python SDK added and locked.
- [x] Hosted Checkout Sessions use server-owned pack definitions and Stripe Price IDs.
- [x] The browser selects only a stable pack ID; it cannot choose credits, currency, or price.
- [x] Checkout creation uses a client idempotency key and a provider idempotency key.
- [x] Payment orders are stored durably before contacting Stripe.
- [x] Webhook signatures are verified against the raw request body.
- [x] Checkout completion and delayed-payment success events fulfill credits.
- [x] Delayed-payment failures and expired sessions update order state.
- [x] Webhook event IDs and credit-ledger idempotency keys prevent repeated fulfillment.
- [x] Partial and full refunds reverse the corresponding amount of credits.
- [x] The credits page can display configured packs and redirect to hosted Checkout.
- [x] Migration `022` creates payment-order and webhook-receipt tables.

Primary files:

- `src/landppt/services/billing_service.py`
- `src/landppt/web/billing_routes.py`
- `src/landppt/web/templates/pages/account/user_credits.html`

This payment implementation is not considered production-complete until the recovery, reconciliation, refund-policy, tax, live-mode, and operational items in `IMPLEMENTATION_PLAN.md` are complete.

### Miscellaneous

- [x] The package CLI entry point now resolves a real `main()` function.
- [x] Application version is read from installed package metadata.
- [x] New security and billing configuration is documented in `.env.example`.

## Test evidence

Focused suites observed passing during implementation:

| Area | Result |
| --- | ---: |
| Authentication hardening | Included in a 24-test focused auth run, all passed |
| SSRF and URL flows | 22 passed; one existing PyPDF2 warning |
| Sandboxed generated HTML | 73 passed, 8 skipped |
| Secret/config/auth regression group | 57 passed |
| Billing/ledger/auth/route regression group | 35 passed |

Most recent focused billing command:

```bash
uv run pytest -q \
  tests/test_billing_service.py \
  tests/test_credits_atomicity.py \
  tests/test_secret_storage_and_verification.py \
  tests/test_auth_security_hardening.py \
  tests/test_web_route_modularization.py
```

Latest rerun result: `35 passed in 2.58s`.

Initial full-suite baseline before these changes:

```text
949 passed, 47 failed, 16 skipped
```

Previous complete-suite result after the Stripe implementation, before the regression repair:

```text
982 passed, 45 failed, 16 skipped, 27 warnings, 8 subtests passed
```

Latest complete-suite result:

```text
1120 passed, 19 skipped, 11 warnings, 8 subtests passed in 48.78s
```

The 45 failures from the previous run were fixed in shipped code: prompt and header/footer contracts, repository-relative paths, migration `022`, the missing `tests/Qwen3-TD-TTS.json` workflow fixture, free-template selection, user-scoped image-cache clearing, the stable system-prompt cache prefix, lifespan cancellation, MiniMax configuration, OpenAI image `response_format`, outline time context, runtime system-prompt pass-through, placeholder SVG self-validation, and portable subtitle paths. `git diff --check` is clean and `python -m compileall -q src/landppt` reports no errors. Both `landppt --help` and `landppt-cli --help` exit 0 without serving.

SQLite migration tests drive `DatabaseMigration.migrate_up` / `migrate_down`: fresh `001`–`022`, upgrade from a schema whose migration record stops at `018`, rollback of `022`, and a forced secret-encryption failure that leaves plaintext and ciphertext bytes unchanged and raises from `run_startup_migrations`. The same fresh and `018` upgrade paths, plus concurrent credit consumption, passed again on PostgreSQL 16.2 (`2 passed in 3.65s`). PostgreSQL migrators take one session advisory lock so the chart's per-pod migrate initContainers cannot apply the same migration at once.

Production configuration tests construct `AppConfig` directly. With `LANDPPT_DEPLOYMENT_MODE=production`, startup calls `validate_production_config` before `run_startup_initialization`, and `landppt-cli worker` calls the same check before `run_worker`. Development mode still accepts the local defaults. The production chart sets `LANDPPT_DEPLOYMENT_MODE=production` and refuses to render if that value is missing or not `production`/`prod`.

`GET /ready` is public. An unauthenticated request reaches the readiness handler, so a down database, cache, or storage returns 503 instead of a login redirect. Web liveness and readiness probes send the first explicit allowed host as the `Host` header, which is what TrustedHost checks. The production NetworkPolicy selects only `web` and `worker` pods and allows ingress on the web port from any source, so kubelet probes and an ingress controller can reach `/ready`. Egress stays limited to DNS, in-namespace PostgreSQL, Valkey, and MinIO, and public TCP 443. PostgreSQL, Valkey, and MinIO are not selected, so their own ports stay reachable. The chart does not render a migrate Job: Job pod templates are immutable and a fixed name would fail the next upgrade. Web and worker initContainers run `migrate-and-bootstrap` after PostgreSQL accepts connections, and those initContainers read the release ConfigMap and Secret. A live multi-replica rollout was not executed.

Initial global quality baseline:

- Black reported 311 files requiring formatting.
- Flake8 reported 16,547 findings, including some real undefined-name defects mixed with legacy style noise.

Do not mass-format the entire repository as part of this effort. Restrict formatting to deliberately changed files and introduce broader style cleanup separately.

## Known gaps and risks

### Release-blocking technical gaps

- [x] Finish the interrupted regression repair, run its focused tests, then rerun the full suite and fix or classify every remaining failure.
- [x] Test all migrations on both a fresh database and an upgraded existing database, including PostgreSQL.
- [x] Add fail-fast production configuration validation.
- [x] Complete Helm wiring for public URL, allowed hosts, trusted proxies, encryption keys, and Stripe secrets/catalog.
- [x] Add payment webhook recovery and reconciliation for concurrency, interruption, and provider-timeout cases.
- [x] Define refund behavior when purchased credits have already been spent; the current implementation can produce a negative balance.
- [x] Propagate stable idempotency keys through slide regeneration, AI slide edit, element edit, native dialog, outline optimization, image regeneration, automatic slide images, bullet enhancement, the slide-edit agent, PPT creation, global and free template generation and adjustment, outline generation, speech scripts, and the unattended outline and speech stages. The synchronous requirements-confirmation file/url generator is disabled; its remaining branch reserves before that model call.
- [x] Add reserve/commit/release semantics for those workflows. Credits are held before the model call, committed only for successful units, and released on failure, cancellation, or expiry. The web process and the worker sweep expired holds. An in-progress hold does not start another model call, and releasing then reserving the same operation id returns the balance.
- [x] Split liveness from dependency-aware readiness. Web `/health` does not check dependencies. Unauthenticated `GET /ready` and `landppt-cli ready` do, and the web probe Host header is an allowed host. A live multi-replica rollout was not executed.
- [ ] Protect operational metrics and define monitoring/alerting ownership. Anonymous `GET /metrics` is no longer successful. Alert routing and an on-call owner are still unset.
- [x] Add and verify database and object-storage backup/restore procedures. The verified rehearsal is local SQLite plus a local artifact directory. The production backup destination remains an external dependency.

### Product, privacy, and legal gaps

- [ ] Terms of service, privacy policy, acceptable-use policy, refund policy, and cookie disclosure.
- [ ] Explicit policy acceptance and version tracking during registration.
- [x] User data export, account deletion, and retention/deletion jobs. Export and deletion are limited to the authenticated user. Payment orders and credit ledger rows are retained and the profile is anonymized. Legal retention exceptions beyond that ledger are not a substitute for counsel.
- [ ] Payment receipt/invoice and tax behavior.
- [ ] Support and escalation workflow for failed payments and account issues.
- [ ] Final accessibility, mobile, and browser validation.
- [ ] Onboarding, sample project, transparent pricing, and payment-state UX.

Legal text cannot be finalized without the operating entity, jurisdiction, tax model, refund terms, data processors, and appropriate legal review.

## Spent-credit refund rule

This is the implemented ledger rule, not a legal refund policy.

- A partial Stripe refund reverses `floor(purchased_credits * refunded_minor / charged_minor)` credits. Refunding 1 minor unit of a 1000-minor, 250-credit pack reverses 0 credits. Refunding half reverses 125.
- A refund that covers the charge, or sets Stripe's `refunded` flag, reverses the entire grant.
- Credits the customer already spent are still reversed. `credits_balance` may become negative. Each reversal is a `refund_reversal` ledger row, and the sum of that user's ledger amounts stays equal to `credits_balance`.
- The same Stripe event does not apply twice. A later event reverses only the increase in the cumulative refunded amount.
- A dispute reverses by the same formula and is keyed by the dispute id, so `charge.dispute.created` and `charge.dispute.funds_withdrawn` for one dispute do not double-debit. A dispute closed as `won` restores that dispute's debit once. A lost dispute stays reversed.
- An admin support adjustment is an `admin_adjust` ledger row recording the actor id, the reason, and an idempotency key. Repeating the key does not apply it again.
- Test-mode and live-mode events do not fulfill or reverse each other.
- Webhook receipts store a sanitized summary and the provider request id. A receipt left `processing` can be finished after its lease expires. A delivery that loses the claim returns HTTP 409 and does not acknowledge success. `landppt-cli reconcile-payments` repairs orders from those stored summaries. Completed receipts may be purged after 90 days. Failed receipts are kept.
- Slide regeneration, AI slide edit, element edit, native dialog, outline optimization, image regeneration, automatic slide images, bullet enhancement, the slide-edit agent, PPT creation, global and free template generation and adjustment, outline generation, speech-script generation and humanizing, and the unattended outline and speech stages reserve a stable operation id before the model call. Only a newly created hold runs the model. Successful units are committed; failure, cancellation, and expiry release the hold. The release ledger key includes the attempt, so reserve/release/reserve/release of one operation id returns the balance. The web lifespan and the worker sweep expired holds on a timer.

### External launch dependencies

- [ ] Production domain and TLS termination.
- [ ] Stripe live account, live Price IDs, API key, webhook endpoint, and webhook signing secret.
- [ ] Transactional email domain with SPF, DKIM, and DMARC.
- [ ] Production PostgreSQL, Valkey, and object storage or an approved managed alternative.
- [ ] Backup destination and restore-drill owner.
- [ ] Error tracking, metrics, alert routing, and on-call owner.
- [ ] Merchant/legal entity information, customer-support contact, and tax decisions.
