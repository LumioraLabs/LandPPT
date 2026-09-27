from click.testing import CliRunner


def test_cli_migrate_runs_database_migrations(monkeypatch):
    from landppt import cli as cli_module

    calls = []

    async def fake_init_db(*, bootstrap_admin=True):
        calls.append(("init_db", bootstrap_admin))

    class FakeMigrationManager:
        async def migrate_up(self):
            calls.append(("migrate_up",))
            return True

    async def fake_close_db():
        calls.append(("close_db",))

    monkeypatch.setattr(cli_module, "init_db", fake_init_db)
    monkeypatch.setattr(cli_module, "migration_manager", FakeMigrationManager())
    monkeypatch.setattr(cli_module, "close_db", fake_close_db)

    result = CliRunner().invoke(cli_module.cli, ["migrate"])

    assert result.exit_code == 0
    assert calls == [("init_db", False), ("migrate_up",), ("close_db",)]
    assert "Database migration completed" in result.output


def test_cli_migrate_and_bootstrap_runs_steps_in_order(monkeypatch):
    from landppt import cli as cli_module

    calls = []

    async def fake_init_db(*, bootstrap_admin=True):
        calls.append(("init_db", bootstrap_admin))

    class FakeMigrationManager:
        async def migrate_up(self):
            calls.append(("migrate_up",))
            return True

    async def fake_templates():
        calls.append(("templates",))
        return [1, 2]

    def fake_admin():
        calls.append(("admin",))

    async def fake_close_db():
        calls.append(("close_db",))

    monkeypatch.setattr(cli_module, "init_db", fake_init_db)
    monkeypatch.setattr(cli_module, "migration_manager", FakeMigrationManager())
    monkeypatch.setattr(cli_module, "ensure_default_templates_exist", fake_templates)
    monkeypatch.setattr(cli_module, "_bootstrap_admin", fake_admin)
    monkeypatch.setattr(cli_module, "close_db", fake_close_db)

    result = CliRunner().invoke(cli_module.cli, ["migrate-and-bootstrap"])

    assert result.exit_code == 0
    assert calls == [
        ("init_db", False),
        ("migrate_up",),
        ("close_db",),
        ("templates",),
        ("close_db",),
        ("admin",),
    ]
    assert "Migration and bootstrap completed: 2 templates available" in result.output


def test_cli_migrate_fails_when_migration_fails(monkeypatch):
    from landppt import cli as cli_module

    async def fake_init_db(*, bootstrap_admin=True):
        return None

    class FakeMigrationManager:
        async def migrate_up(self):
            return False

    async def fake_close_db():
        return None

    monkeypatch.setattr(cli_module, "init_db", fake_init_db)
    monkeypatch.setattr(cli_module, "migration_manager", FakeMigrationManager())
    monkeypatch.setattr(cli_module, "close_db", fake_close_db)

    result = CliRunner().invoke(cli_module.cli, ["migrate"])

    assert result.exit_code != 0
    assert "Database migration failed" in result.output


def test_worker_refuses_unsafe_production_config_before_starting(monkeypatch):
    from landppt import cli as cli_module
    from test_production_config import _SECRET, _STRIPE_KEY, _WEBHOOK_SECRET, _config

    started = []

    async def fake_run_worker(queue_name=None):
        started.append(queue_name)

    async def fake_close_db():
        started.append("close")

    monkeypatch.setattr("landppt.tasks.worker.run_worker", fake_run_worker)
    monkeypatch.setattr(cli_module, "close_db", fake_close_db)
    monkeypatch.setattr(
        "landppt.core.config.app_config",
        _config(secret_key="short", stripe_secret_key=_STRIPE_KEY),
    )

    result = CliRunner().invoke(cli_module.cli, ["worker"])

    assert result.exit_code != 0
    assert started == []
    message = f"{result.output}\n{result.exception}"
    assert "SECRET_KEY" in message
    assert _SECRET not in message
    assert _STRIPE_KEY not in message
    assert _WEBHOOK_SECRET not in message


def test_worker_starts_after_valid_production_config(monkeypatch):
    from landppt import cli as cli_module
    from test_production_config import _config

    started = []

    async def fake_run_worker(queue_name=None):
        started.append(queue_name)

    async def fake_close_db():
        started.append("close")

    monkeypatch.setattr("landppt.tasks.worker.run_worker", fake_run_worker)
    monkeypatch.setattr(cli_module, "close_db", fake_close_db)
    monkeypatch.setattr("landppt.core.config.app_config", _config())

    result = CliRunner().invoke(cli_module.cli, ["worker", "--queue", "slides"])

    assert result.exit_code == 0, result.output
    assert started == ["slides", "close"]
