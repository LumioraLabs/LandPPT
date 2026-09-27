"""Account export, deletion, and unauthenticated metrics."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_export_and_delete_do_not_touch_another_user(tmp_path):
    from landppt.database.models import Base, Project, User
    from landppt.services.account_data_service import (
        delete_user_account,
        export_user_data,
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'accounts.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            owner = User(
                username="owner",
                email="owner@example.com",
                credits_balance=4,
                is_active=True,
            )
            owner.set_password("owner-password-ok")
            other = User(
                username="other",
                email="other@example.com",
                credits_balance=9,
                is_active=True,
            )
            other.set_password("other-password-ok")
            session.add_all([owner, other])
            await session.commit()
            await session.refresh(owner)
            await session.refresh(other)
            session.add_all(
                [
                    Project(
                        project_id="proj-owner",
                        user_id=owner.id,
                        title="Owner deck",
                        scenario="business",
                        topic="Owner topic",
                    ),
                    Project(
                        project_id="proj-other",
                        user_id=other.id,
                        title="Other deck",
                        scenario="business",
                        topic="Other topic",
                    ),
                ]
            )
            await session.commit()
            owner_id = owner.id
            other_id = other.id

        async with maker() as session:
            exported = await export_user_data(session, owner_id)
        assert exported["user"]["username"] == "owner"
        assert "password_hash" not in exported["user"]
        assert [project["project_id"] for project in exported["projects"]] == [
            "proj-owner"
        ]

        async with maker() as session:
            assert await delete_user_account(session, owner_id, "wrong-password") is False
        async with maker() as session:
            assert await delete_user_account(session, owner_id, "owner-password-ok") is True
            owner_projects = (
                await session.scalars(select(Project).where(Project.user_id == owner_id))
            ).all()
            other_project = await session.scalar(
                select(Project).where(Project.project_id == "proj-other")
            )
            owner_row = await session.get(User, owner_id)
            other_row = await session.get(User, other_id)
        assert owner_projects == []
        assert other_project.title == "Other deck"
        assert other_row.credits_balance == 9
        assert other_row.is_active is True
        assert owner_row.is_active is False
        assert owner_row.email is None
        assert owner_row.username == f"deleted-{owner_id}"
    finally:
        await engine.dispose()


def test_unauthenticated_metrics_and_export_are_not_successful(monkeypatch):
    import landppt.main as main_module

    async def fake_startup():
        return True

    monkeypatch.setattr(main_module, "run_startup_initialization", fake_startup)
    client = TestClient(main_module.app, follow_redirects=False)
    metrics = client.get("/metrics")
    exported = client.get("/api/account/export")
    assert metrics.status_code not in {200, 204}
    assert exported.status_code not in {200, 204}
    assert "Owner deck" not in metrics.text
    assert "Owner deck" not in exported.text
