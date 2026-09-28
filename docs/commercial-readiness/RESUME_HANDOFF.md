# Resume Handoff

Last updated: 2026-09-27 (America/Toronto)

## Repository state

- Repository: `/home/yangyangli/projects/lumiora/LandPPT`
- Branch: `feat/commercial-readiness`
- PR: `https://github.com/LumioraLabs/LandPPT/pull/1`
- PR head before this review: `d3c558dc2506b252df4d12b73c0e33c7037a3a3b`
- Base: `master` at `19971a1120a42003658b3782666843b5ecbe7144`
- Worktree: review fixes are uncommitted; preserve them

The PR had no GitHub review comments, reviews, or status checks when inspected. Do not merge it merely because the local suite passes: the external launch gates below remain open.

## Review fixes in the current worktree

- Account deletion invalidates cached sessions, clears the browser cookie, deletes stored artifact objects, removes non-financial owned rows, and anonymizes direct identifiers while retaining financial audit rows.
- Account export no longer caps the credit ledger at 10,000 rows and includes mapped project, billing, reservation, configuration, task, artifact, notification, template, invite, and verification metadata without authentication secrets.
- Normal project deletion removes local/S3 artifact bytes before deleting metadata.
- API login/logout now set and clear the centralized session cookie.
- Login throttling is consumed before expensive password verification, including successful attempts after the budget is exhausted.
- Protected requests validate sessions against the database; stale cache entries are no longer an authorization source.
- Stripe webhook bodies have a 1 MiB hard limit.
- Checkout retries reuse the same idempotency key until the server responds.
- Credits-page API text uses DOM `textContent` rather than HTML interpolation.
- Long-running billable work renews its credit reservation; the expiry sweeper rechecks the lease under lock before releasing it.
- PostgreSQL migrations keep the same physical connection checked out while holding the session advisory lock.
- `/metrics` requires a dedicated bearer token; production validates its strength and Helm ServiceMonitor reads it from a Secret.
- Docker publication now depends on the full test/compile/whitespace quality job; repository write permission is isolated to the post-build deployment job.

## Verification evidence

Latest complete local suite:

```text
1130 passed, 19 skipped, 11 warnings, 8 subtests passed in 56.31s
```

Also passed:

```bash
uv run python -m compileall -q src tests
git diff --check
uv run landppt --help
uv run landppt-cli --help
```

Focused review suites covered account privacy, authentication, billing/webhooks, reservation renewal, project artifact deletion, production configuration, Helm rendering, metrics authentication, and migration regressions.

The 19 skipped tests include PostgreSQL-dependent coverage when `LANDPPT_TEST_POSTGRES_URL` is not configured. SQLite migration coverage is verified locally. Do not claim a current PostgreSQL rehearsal until the PostgreSQL suite is run against a real instance.

## Resume commands

```bash
git status --short
git diff --check
git diff --stat
uv run python -m compileall -q src tests
uv run pytest -q
```

Before committing, review the complete diff and verify the PR comparison:

```bash
git diff --check origin/master
git diff --stat origin/master
git diff origin/master -- .github helm src tests docs/commercial-readiness
```

## Remaining P0 work

1. Run fresh-install, upgrade, rollback, advisory-lock, and credit/payment concurrency tests against PostgreSQL; current local PostgreSQL tests are skipped.
2. Review the OAuth deletion confirmation UX and add a cooling-off period if the product/legal policy requires one.
3. Define retention for legacy files outside artifact storage and verify deletion of every legacy narration/generated-file path.
4. Run Stripe test-mode end-to-end purchase, delayed payment, duplicate delivery, refund, dispute, and reconciliation exercises; then run a small live purchase/refund rehearsal.
5. Add failed-payment/unfulfilled-order alert routing and name monitoring/on-call owners.
6. Configure production domain/TLS, PostgreSQL, Valkey, object storage, backup destination, restore drill, RPO/RTO, and rollback runbooks.
7. Complete legal policies, consent-version storage, tax/invoice decisions, support/refund procedures, and merchant details.
8. Verify email SPF/DKIM/DMARC and deliverability.
9. Complete accessibility, responsive, supported-browser, performance, onboarding, pricing, and trust-page acceptance.
10. Add remaining supply-chain gates: dependency/secret/container scanning, SBOM, static/type checks after establishing a clean baseline, and staged rollout verification.

## Important cautions

- The repository-wide Black/isort baseline is not clean. Do not add a mandatory global formatting gate without first separating legacy formatting cleanup.
- The auth routes file has a larger formatting delta than the logical changes; review it carefully before commit.
- Account/project storage deletion intentionally fails if artifact rows name a backend different from the active configured backend. This prevents silently orphaning bytes but requires an operational migration plan when storage backends change.
- Account deletion spans external object deletion and multiple database commits through project deletion. It is retryable but not globally atomic.
- User export is synchronous and unbounded; very large accounts may need an asynchronous export job before scale launch.
- Legal, operational, and live-environment launch gates cannot be satisfied by unit tests alone.
