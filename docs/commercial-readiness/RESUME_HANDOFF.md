# Resume Handoff

Last updated: 2026-09-26 (America/Toronto)

## Repository state

- Repository: `/home/yangyangli/projects/lumiora/LandPPT`
- Branch: `master`
- Goal state: Phase 1 resumed and implemented in the worktree
- Worktree: heavily modified and uncommitted
- Destructive cleanup/reset is not authorized
- No commits or pushes were made during this implementation pass

The worktree also contains a partially completed post-suite regression repair in `src/landppt/services/prompts/design_prompts.py` and three test files. The repair was interrupted before formatting or tests. Review semantically; do not assume the newest edits are correct merely because `git diff --check` passes.

## New files in this implementation pass

```text
src/landppt/auth/cookies.py
src/landppt/auth/passwords.py
src/landppt/auth/security.py
src/landppt/core/secret_storage.py
src/landppt/services/billing_service.py
src/landppt/utils/safe_fetch.py
src/landppt/web/billing_routes.py
tests/test_auth_security_hardening.py
tests/test_billing_service.py
tests/test_credits_atomicity.py
tests/test_safe_fetch.py
tests/test_secret_storage_and_verification.py
tests/test_untrusted_frame_sandbox.py
```

This handoff directory is also new:

```text
docs/commercial-readiness/
```

## Resume sequence

1. Read, in order:

   - `docs/commercial-readiness/STATUS.md`
   - `docs/commercial-readiness/IMPLEMENTATION_PLAN.md`
   - this handoff

2. Confirm the worktree without modifying it:

```bash
git status --short
git diff --check
git diff --stat
```

3. Compile-check the application:

```bash
uv run python -m compileall -q src/landppt
```

4. Run the most recent focused gate:

```bash
uv run pytest -q \
  tests/test_billing_service.py \
  tests/test_credits_atomicity.py \
  tests/test_secret_storage_and_verification.py \
  tests/test_auth_security_hardening.py \
  tests/test_web_route_modularization.py
```

Expected last-observed result: `35 passed`.

5. The interrupted prompt, header/footer, path, and migration-version repair has been retested. The focused batch below passed (`150 passed`):

```bash
uv run pytest -q \
  tests/test_cleanup_excess_slides_security.py \
  tests/test_export_task_security.py \
  tests/test_unattended_mode.py \
  tests/test_design_richness_prompts.py \
  tests/test_header_footer_extraction.py
```

Locked-zone prompts now include the standalone marker `**稳定区域理解方向**`.

6. Then run the full suite:

```bash
uv run pytest -q
```

Last authoritative result:

```text
1120 passed, 19 skipped, 11 warnings, 8 subtests passed in 48.78s
```

The baseline before this hardening pass was `949 passed, 47 failed, 16 skipped`. The result immediately before the regression repair was `982 passed, 45 failed, 16 skipped`. Do not conceal regressions by deleting or weakening valid tests.

Webhook recovery, spent-credit refunds, credit reservations, private metrics, account export and deletion, and the local backup rehearsal are now in this same worktree. The spent-credit rule is in `STATUS.md`. Slide regeneration, AI editing, template generation and adjustment, outline generation, speech scripts, and the unattended outline and speech stages reserve before the model call and release the hold on cancellation or expiry. Legal, tax, live Stripe, email DNS, and on-call boxes stay open.

A later verification pass fixed five production gaps in this same worktree: `GET /ready` is public and the web probe sends an allowed Host; the NetworkPolicy selects only web and worker and allows ingress on the web port so kubelet probes and ingress controllers are not blocked; migrations run in web and worker initContainers after in-chart PostgreSQL and the release Secret exist, with no release Job and no pre-install hook; production values set `LANDPPT_DEPLOYMENT_MODE=production` and the chart guard requires it; `landppt-cli worker` validates production configuration before it consumes the queue. Paid launch is still blocked by the later phases.

7. Continue the Phase 1 regression repair described below.

## Last full-suite failure inventory

The 45 failures from the last complete run were classified as follows:

| Category | Count | State at pause |
| --- | ---: | --- |
| Tests hardcoding obsolete `/root/clawd` paths | 5 | Test paths changed to repository-relative paths; not rerun |
| Stale assertion that migration `018` must be last | 1 | Updated to `022`; not rerun |
| Missing `tests/Qwen3-TD-TTS.json` workflow fixture/default asset | 2 | Not fixed |
| Design richness prompt contract | 5 | Partial code repair; interrupted and untested |
| Header/footer extraction and locked-zone contract | 5 | Partial code repair; interrupted and untested |
| Free-template selection | 1 | Not fixed |
| User-scoped image-gallery cache clearing | 1 | Not fixed |
| Image system-prompt cache prefix stability | 1 | Not fixed |
| Lifespan cancellation/shutdown handling | 1 | Not fixed |
| MiniMax configuration/provider/factory behavior | 15 | Not fixed |
| OpenAI image `response_format` behavior | 2 | Not fixed |
| Outline current-time/quarter context | 2 | Not fixed |
| Runtime provider system-prompt composition expectations | 2 | Needs code/test contract review; not fixed |
| Placeholder SVG self-validation | 1 | Not fixed |
| Video subtitle path expectation | 1 | Needs portability contract review; not fixed |

The prompt repair added `_build_content_quality_context`, header/footer extraction with BeautifulSoup, richer locked-zone guidance, explicit four-grid avoidance, and complete-information-unit wording. It did **not** receive a compile, format, or test pass after the interrupted edit.

## Recommended immediate implementation order

### A. Full-suite regression pass

Finish and test the interrupted prompt/path repair, then work down the classified failure inventory. Rerun the complete suite after each coherent repair batch.

### B. Production validator

Add an explicit deployment/production-mode validation function and call it during startup before migrations or serving requests. Cover at least:

- placeholder/weak `SECRET_KEY`;
- missing `LANDPPT_CONFIG_ENCRYPTION_KEYS`;
- enabled/default bootstrap admin;
- non-HTTPS/missing public base URL;
- wildcard hosts;
- insecure cookies or disabled CSRF origin checks;
- missing trusted proxies where forwarded headers are used;
- incomplete Stripe settings or empty/invalid pack catalog when billing is enabled;
- billing enabled while credits enforcement is disabled.

Add unit tests that construct `AppConfig` objects directly; avoid mutating the global config where possible.

### C. Helm production wiring

Work was paused while inspecting:

- `helm/landppt/values.yaml`
- `helm/landppt/values-production.yaml`
- `helm/landppt/templates/secret.yaml`
- `helm/landppt/templates/configmap.yaml`
- `helm/landppt/templates/deployment.yaml`
- `helm/landppt/templates/networkpolicy.yaml`

The production overlay currently enables credits and browser security, but it does not yet provide complete public URL, host/proxy, encryption, or billing settings. The chart already supports `app.existingSecret`; document and validate the required secret keys rather than committing real secrets.

### D. Payment concurrency/recovery review

Before considering Checkout production-ready, review these areas in `src/landppt/services/billing_service.py`:

- concurrent delivery of the same event;
- behavior when one delivery reports a duplicate while the winning transaction later fails;
- reclaiming stale `processing` webhook receipts;
- reconciliation after Stripe succeeds but the local response/commit fails;
- provider timeouts during Checkout creation;
- negative credit balances after refunds;
- charge dispute events;
- retention of webhook receipts and payment orders.

Add PostgreSQL-backed integration coverage for concurrency-sensitive behavior.

### E. Billable-operation idempotency

Search every credit usage path:

```bash
rg -n "consume_credits|consume_credits_for_operation|add_credits" src/landppt
```

Many callers still do not supply stable idempotency keys. Define operation IDs and reserve/commit/refund behavior before expanding paid access.

## Important implementation details

### Stripe configuration

The following configuration names were introduced:

```text
LANDPPT_BILLING_ENABLED
LANDPPT_BILLING_CREDIT_PACKS_JSON
LANDPPT_BILLING_ALLOW_PROMOTION_CODES
LANDPPT_BILLING_AUTOMATIC_TAX_ENABLED
STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET
```

The pack catalog is a JSON object keyed by a stable pack ID. Each entry requires:

```json
{
  "name": "Starter",
  "credits": 500,
  "amount_minor": 999,
  "currency": "usd",
  "stripe_price_id": "price_..."
}
```

Never accept the amount, currency, credit grant, or Stripe Price ID from the browser or webhook metadata. The local `PaymentOrder` is the fulfillment authority.

### Stripe events currently handled

```text
checkout.session.completed
checkout.session.async_payment_succeeded
checkout.session.async_payment_failed
checkout.session.expired
charge.refunded
```

The webhook is public at `/billing/webhooks/stripe`, but it verifies the Stripe signature using the unmodified request body.

### Migrations introduced

```text
019 add_credit_transaction_idempotency
020 encrypt_stored_config_secrets
021 harden_verification_codes
022 add_payment_orders
```

### Secret storage

Encrypted values use the `enc:v1:` prefix. MultiFernet reads all configured keys and writes with the first, enabling rotation. Legacy plaintext remains readable during transition, and migration `020` encrypts it.

### Generated HTML sandbox

The iframe policy intentionally uses `sandbox="allow-same-origin"` without `allow-scripts`. Adding `allow-scripts` while retaining `allow-same-origin` would materially weaken isolation and must not be done without redesigning the frame origin model.

## Commands useful for review

```bash
rg -n "srcdoc|contentDocument|contentWindow|sandbox=" src/landppt/web
rg -n "aiohttp|httpx|requests|urlopen|curl" src/landppt
rg -n "UserConfig|config_value|client_secret|smtp_password|api_key" src/landppt --glob '*.py'
rg -n "consume_credits|add_credits|credits_balance" src/landppt --glob '*.py'
rg -n "billing|payment|checkout|stripe|refund" src/landppt tests
```

## External information needed before launch

The owner will eventually need to supply or decide:

- production domain and reverse-proxy topology;
- Stripe live account, product/Price IDs, live secret key, and webhook secret;
- legal business name, address/jurisdiction, tax handling, and refund terms;
- transactional email domain/provider and DNS records;
- production database, cache, and object-storage providers;
- monitoring/error-tracking destination and alert owners;
- backup destination, retention, RPO, RTO, and restore-drill owner;
- support email, payment escalation owner, and incident/on-call process.

## Suggested resume prompt

```text
Resume the paused commercial-readiness implementation. Read docs/commercial-readiness/README.md, STATUS.md, IMPLEMENTATION_PLAN.md, and RESUME_HANDOFF.md first. Preserve the current uncommitted worktree. The last full suite was 982 passed, 45 failed, 16 skipped. Finish and test the interrupted design-prompts/path/migration repair first, then work through the classified failure inventory and continue Phase 1 without resetting or discarding existing changes.
```
