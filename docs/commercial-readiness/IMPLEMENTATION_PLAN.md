# Commercial Readiness Implementation Plan

Last updated: 2026-09-26 (America/Toronto)

## Release objective

Reach a state where LandPPT can safely invite users to register, use paid features, and purchase credits with predictable security, billing, privacy, reliability, and support behavior.

No public paid launch should occur until every **P0 release gate** below is complete and evidenced.

## Phase 1 — P0 correctness and production configuration

### 1.1 Full regression gate

- [ ] Run `uv run pytest -q` against the entire repository.
- [ ] Record every failing test by category: regression, stale expectation, environment-only, or unrelated pre-existing defect.
- [ ] Fix all security, authentication, authorization, billing, migration, data-integrity, and primary-user-flow failures.
- [ ] Re-run the focused security suites after each fix batch.
- [ ] Run `git diff --check` and compile checks.
- [ ] Verify both CLI entry points without unintentionally starting a server during `--help`.

Exit criteria:

- Full suite is green, or every remaining skip/failure has a written, owner-approved disposition that cannot affect production.
- No compile errors or whitespace errors.

### 1.2 Database and migration validation

- [ ] Create a fresh SQLite database and migrate from `001` through `022`.
- [ ] Upgrade a fixture representing the last released schema through `022`.
- [ ] Repeat both paths on PostgreSQL.
- [ ] Verify rollback behavior for reversible migrations.
- [ ] Verify that secret-encryption migration failures stop startup without corrupting plaintext or ciphertext.
- [ ] Test concurrent payment/credit operations on PostgreSQL, not only SQLite.

Exit criteria:

- Fresh and upgraded installations produce the expected schema and data.
- Failed migrations are observable, retryable, and do not create split-brain schema state.

### 1.3 Fail-fast production validation

- [ ] Introduce an explicit production/deployment mode.
- [ ] Reject placeholder or short `SECRET_KEY` values in production.
- [ ] Require a dedicated encryption key set in production.
- [ ] Reject default bootstrap-admin credentials; disable bootstrap after initial provisioning.
- [ ] Require HTTPS public base URL, secure cookies, CSRF origin checks, and explicit allowed hosts.
- [ ] Require trusted proxy CIDRs when forwarded headers are expected.
- [ ] Reject wildcard CORS with credentialed production traffic.
- [ ] When billing is enabled, require credits enforcement, valid Stripe secrets, and a non-empty validated pack catalog.
- [ ] Refuse production SQLite unless an explicit development exception exists.

Exit criteria:

- An insecure production configuration cannot boot.
- Error messages name the missing setting without printing secret values.

### 1.4 Helm and runtime hardening

- [ ] Add production values for public URL, allowed hosts, proxy CIDRs, encryption keys, and billing flags.
- [ ] Document the required existing-secret keys, including Stripe and encryption material.
- [ ] Enable and tighten production NetworkPolicy.
- [ ] Ensure migration jobs receive required secrets and complete before application rollout.
- [ ] Separate liveness and readiness probes.
- [ ] Make readiness validate database, cache/queue, and required storage dependencies.
- [ ] Confirm rolling deployment behavior with database migrations and multiple web/worker replicas.
- [ ] Add chart rendering tests for missing and complete production secrets.

Exit criteria:

- A production chart renders only with all mandatory values.
- Web and worker rollouts cannot serve traffic before dependencies and migrations are ready.

## Phase 2 — P0 billing and usage integrity

### 2.1 Payment recovery and reconciliation

- [ ] Test simultaneous deliveries of the same event.
- [ ] Ensure a delivery that loses a race does not return success if the winner later fails.
- [ ] Add a lease/retry policy for webhook receipts left in `processing`.
- [ ] Add a reconciliation command/job for Stripe sessions, payments, refunds, and local orders.
- [ ] Persist provider request IDs needed for customer support.
- [ ] Define event-receipt retention and cleanup.
- [ ] Alert on repeated failed receipts or unfulfilled paid orders.
- [ ] Verify delayed payment success/failure behavior.
- [ ] Verify test-mode and live-mode isolation.

Exit criteria:

- Replaying, reordering, duplicating, or temporarily failing events cannot grant credits twice or permanently lose a paid purchase.

### 2.2 Refund and dispute policy

- [ ] Decide whether refund reversals may create negative balances.
- [ ] Implement the approved policy for already-spent credits.
- [ ] Handle charge disputes and dispute reversals.
- [ ] Define partial-refund rounding behavior.
- [ ] Prevent customer-facing balances and internal ledgers from diverging.
- [ ] Add an audited admin workflow for support adjustments.

Exit criteria:

- Refunds and disputes are deterministic, auditable, idempotent, and consistent with the published policy.

### 2.3 Usage reservation and charging

- [ ] Inventory every call to `consume_credits` and helper wrappers.
- [ ] Assign a stable operation ID/idempotency key to every billable workflow.
- [ ] Reserve credits before expensive asynchronous work.
- [ ] Commit the correct charge for successful units only.
- [ ] Release or refund reservations on failure, cancellation, timeout, and worker death.
- [ ] Reconcile task/job records against ledger records.
- [ ] Add tests for retries, parallel workers, partial slide completion, and duplicate callbacks.

Exit criteria:

- A user cannot overspend, be double-charged, or receive repeat paid work through retries.
- Failed work follows a documented and tested charging policy.

### 2.4 Customer billing experience

- [ ] Add durable order history to the credits page.
- [ ] Provide understandable pending, failed, expired, refunded, and fulfilled states.
- [ ] Add payment confirmation email or receipt linkage.
- [ ] Configure tax collection and invoices as required by the merchant jurisdiction.
- [ ] Add subscription/customer portal support only if recurring plans are approved.
- [ ] Add admin payment lookup without exposing provider secrets or full payment details.

Exit criteria:

- Customers and support staff can understand and resolve every payment state.

## Phase 3 — P0 privacy, legal, and account lifecycle

### 3.1 Policies and consent

- [ ] Draft and legally review Terms of Service.
- [ ] Draft and legally review Privacy Policy.
- [ ] Draft Acceptable Use Policy.
- [ ] Draft Refund/Cancellation Policy aligned with implemented billing behavior.
- [ ] Draft Cookie/analytics disclosure.
- [ ] Display the effective date and policy version.
- [ ] Require explicit signup acceptance and store the accepted versions/timestamp.

### 3.2 Data rights and retention

- [ ] Build authenticated user data export.
- [ ] Build account deletion with reauthentication and a cooling-off period if appropriate.
- [ ] Define retention for projects, uploaded source documents, generated files, logs, payment records, and deleted accounts.
- [ ] Implement deletion/anonymization jobs for every storage backend.
- [ ] Define legally required exceptions for invoices, fraud prevention, and audit records.
- [ ] Test that ownership checks prevent export/deletion of another user’s data.

### 3.3 Processor and security disclosures

- [ ] Inventory subprocessors: model providers, email, Stripe, storage, analytics, monitoring, and hosting.
- [ ] Document data regions and cross-border transfers.
- [ ] Define a security contact and incident-reporting channel.
- [ ] Prepare DPA/subprocessor materials if selling to organizations.

Exit criteria:

- Public policies match actual application behavior.
- Data requests can be completed and evidenced without manual database improvisation.

## Phase 4 — P0 operations and reliability

### 4.1 Observability

- [ ] Add structured JSON logging with request, user-safe, task, order, and provider request identifiers.
- [ ] Ensure logs never contain passwords, tokens, provider keys, verification codes, raw payment payloads, or sensitive documents.
- [ ] Integrate error tracking.
- [ ] Protect metrics from public anonymous access.
- [ ] Add dashboards and alerts for error rate, latency, queue depth, worker failures, database saturation, storage errors, email failures, and payment failures.
- [ ] Assign alert and on-call ownership.

### 4.2 Backup and disaster recovery

- [ ] Automate PostgreSQL backups with encryption and retention.
- [ ] Back up or version object storage as required.
- [ ] Test a complete restore into an isolated environment.
- [ ] Record measured RPO and RTO.
- [ ] Create runbooks for database loss, object loss, secret rotation, payment outage, queue failure, and bad migration.

### 4.3 CI/CD and supply chain

- [ ] Add CI gates for unit/integration tests, formatting checks, type/static analysis, and migration checks.
- [ ] Add dependency vulnerability scanning and secret scanning.
- [ ] Add container scanning and SBOM generation.
- [ ] Pin/review production image tags and base images.
- [ ] Add staged deployment and rollback verification.
- [ ] Prevent unreviewed direct production deployment.

Exit criteria:

- A failed build, vulnerable artifact, broken migration, or failed rollout is detected before broad customer impact.
- Backups have a recent successful restore drill.

## Phase 5 — P1 product maturity and conversion

### 5.1 Registration and onboarding

- [ ] Clear registration value proposition and verification guidance.
- [ ] First-run setup for AI provider choice, billing model, and sample generation.
- [ ] Provide a sample project and empty-state guidance.
- [ ] Explain credits before users encounter insufficient-balance failures.
- [ ] Add safe, privacy-aware product analytics funnels.

### 5.2 Marketing and trust

- [ ] Product landing page with concrete outcomes, screenshots, and limits.
- [ ] Transparent pricing and credit consumption examples.
- [ ] FAQ, documentation, support contacts, and status page.
- [ ] Security/privacy page that accurately describes controls.
- [ ] SEO metadata, social cards, canonical URLs, and sitemap.

### 5.3 Quality and accessibility

- [ ] Keyboard navigation and visible focus states.
- [ ] Screen-reader labels and semantic status announcements.
- [ ] Contrast and zoom testing.
- [ ] Responsive tests across supported mobile/tablet/desktop sizes.
- [ ] Cross-browser validation of editor, export, Checkout return, and authentication flows.
- [ ] Performance budgets and load tests for generation, export, and API traffic.

## Final launch gates

- [ ] Full automated test suite passes.
- [ ] PostgreSQL migrations pass fresh-install and upgrade rehearsals.
- [ ] Stripe test-mode end-to-end purchase, delayed payment, duplicate webhook, refund, and dispute scenarios pass.
- [ ] A small-value live-mode purchase and refund rehearsal passes.
- [ ] Security configuration validator passes in production.
- [ ] Backup restore drill passes.
- [ ] Monitoring and alerts have named owners.
- [ ] Legal policies are approved and published.
- [ ] Account export/deletion and retention jobs pass acceptance tests.
- [ ] Email SPF/DKIM/DMARC and deliverability are verified.
- [ ] Accessibility and supported-browser acceptance pass.
- [ ] Incident, support, refund, and rollback runbooks are approved.
- [ ] Owner signs off on launch risk and rollback criteria.

