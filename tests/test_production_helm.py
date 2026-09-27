"""Production chart must fail closed and keep probes, policy, and migrations usable."""

import shutil
import subprocess
from pathlib import Path

import pytest

from test_container_security import read_repo_file

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm" / "landppt"
HELM = shutil.which("helm")

COMPLETE_OVERLAY = """
app:
  existingSecret: landppt-keys
  existingSecretKeys:
    - SECRET_KEY
    - LANDPPT_CONFIG_ENCRYPTION_KEYS
    - STRIPE_SECRET_KEY
    - STRIPE_WEBHOOK_SECRET
    - LANDPPT_BILLING_CREDIT_PACKS_JSON
  env:
    LANDPPT_PUBLIC_BASE_URL: https://slides.example.com
    LANDPPT_ALLOWED_HOSTS: slides.example.com
    LANDPPT_TRUSTED_PROXY_CIDRS: 10.0.0.0/8
    LANDPPT_BILLING_ENABLED: "true"
postgresql:
  auth:
    password: stand-in-not-a-secret
minio:
  auth:
    rootPassword: stand-in-not-a-secret
"""


def test_network_policy_selects_only_web_and_worker_and_allows_probes():
    policy = read_repo_file("helm/landppt/templates/networkpolicy.yaml")
    selector = policy.split("podSelector:", 1)[1].split("policyTypes:", 1)[0]
    assert "matchExpressions" in selector
    assert "app.kubernetes.io/component" in selector
    assert "- web" in selector
    assert "- worker" in selector
    assert "landppt.selectorLabels" not in selector
    ingress = policy.split("ingress:", 1)[1].split("egress:", 1)[0]
    assert "from:" not in ingress
    assert "podSelector: {}" not in ingress
    assert ".Values.service.port" in ingress
    egress = policy.split("egress:", 1)[1]
    assert "port: 5432" in egress
    assert "port: 6379" in egress
    assert "port: 9000" in egress
    assert "port: 443" in egress
    assert "10.0.0.0/8" in egress


def test_chart_does_not_render_an_immutable_migrate_job():
    job = read_repo_file("helm/landppt/templates/migration-job.yaml")
    assert "kind: Job" not in job
    assert "helm.sh/hook" not in job
    assert "migration.hook cannot be true" in job
    assert "initContainers migrate" in job


def test_production_chart_requires_deployment_mode_and_refuses_pre_install_hook():
    guard = read_repo_file("helm/landppt/templates/00-production-guard.yaml")
    values = read_repo_file("helm/landppt/values-production.yaml")
    assert "LANDPPT_DEPLOYMENT_MODE" in guard
    assert "migration.hook=false" in guard
    assert 'LANDPPT_DEPLOYMENT_MODE: "production"' in values
    assert "hook: false" in values


def test_web_and_worker_migrate_after_postgres_and_send_probe_host():
    deployment = read_repo_file("helm/landppt/templates/deployment.yaml")
    worker = read_repo_file("helm/landppt/templates/worker-deployment.yaml")
    helper = read_repo_file("helm/landppt/templates/_helpers.tpl")
    for template in (deployment, worker):
        assert 'include "landppt.migrateInitContainer"' in template
    assert "wait-for-postgres" in deployment.split("migrateInitContainer", 1)[0]
    assert 'include "landppt.renderHttpProbe"' in deployment
    assert '"Host"' in helper
    assert "migrate-and-bootstrap" in helper


def _helm_template(*extra: str) -> subprocess.CompletedProcess[str]:
    if HELM is None:
        pytest.skip("helm is not installed")
    return subprocess.run(
        [HELM, "template", "landppt", str(CHART), *extra],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_production_render_fails_closed_and_complete_render_is_routable(tmp_path):
    bare = _helm_template("-f", "helm/landppt/values-production.yaml")
    assert bare.returncode != 0

    missing = _helm_template(
        "-f",
        "helm/landppt/values-production.yaml",
        "--set",
        "app.secrets.SECRET_KEY=stand-in-secret-key-value",
        "--set",
        "postgresql.auth.password=stand-in",
        "--set",
        "minio.auth.rootPassword=stand-in",
    )
    assert missing.returncode != 0
    assert "LANDPPT_PUBLIC_BASE_URL" in missing.stderr

    overlay = tmp_path / "complete.yaml"
    overlay.write_text(COMPLETE_OVERLAY, encoding="utf-8")
    complete = _helm_template(
        "-f",
        "helm/landppt/values-production.yaml",
        "-f",
        str(overlay),
    )
    assert complete.returncode == 0, complete.stderr
    rendered = complete.stdout
    assert "LANDPPT_DEPLOYMENT_MODE: \"production\"" in rendered or 'LANDPPT_DEPLOYMENT_MODE: "production"' in rendered
    assert "name: landppt-migrate" not in rendered
    assert "pre-install" not in rendered
    assert rendered.count("migrate-and-bootstrap") == 2
    assert "name: wait-for-postgres" in rendered
    assert "name: Host" in rendered
    assert "slides.example.com" in rendered
    policy = rendered.split("kind: NetworkPolicy", 1)[1].split("---", 1)[0]
    component = policy.split("app.kubernetes.io/component", 1)[1].split("policyTypes:", 1)[0]
    assert "- web" in component
    assert "- worker" in component
    assert "postgresql" not in component
    assert "valkey" not in component
    assert "minio" not in component
    ingress = policy.split("ingress:", 1)[1].split("egress:", 1)[0]
    assert "from:" not in ingress
    assert "port: 8000" in ingress
    egress = policy.split("egress:", 1)[1]
    assert "port: 5432" in egress
    assert "port: 443" in egress
    for secret in ("sk_live_", "sk_test_", "whsec_", "Fernet"):
        assert secret not in rendered

    mode = _helm_template(
        "-f",
        "helm/landppt/values-production.yaml",
        "-f",
        str(overlay),
        "--set",
        "app.env.LANDPPT_DEPLOYMENT_MODE=development",
    )
    assert mode.returncode != 0
    assert "LANDPPT_DEPLOYMENT_MODE" in mode.stderr

    hooked = _helm_template(
        "-f",
        "helm/landppt/values-production.yaml",
        "-f",
        str(overlay),
        "--set",
        "migration.hook=true",
    )
    assert hooked.returncode != 0
    assert "migration.hook" in hooked.stderr
