import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from landppt.database.models import Artifact, AsyncTask, Base, Project, User
from landppt.database.repositories import ProjectRepository


class _AsyncConnectionAdapter:
    """为仓库测试提供最小异步连接接口。"""

    def __init__(self, connection):
        self._connection = connection

    async def run_sync(self, fn):
        return fn(self._connection)


class _AsyncSessionAdapter:
    """用同步 Session 模拟仓库依赖的异步 Session。"""

    def __init__(self, session: Session):
        self._session = session

    async def execute(self, stmt, params=None):
        if params is None:
            return self._session.execute(stmt)
        return self._session.execute(stmt, params)

    async def commit(self):
        self._session.commit()

    async def rollback(self):
        self._session.rollback()

    async def connection(self):
        return _AsyncConnectionAdapter(self._session.connection())


@pytest.mark.asyncio
async def test_delete_project_also_cleans_legacy_slide_revisions(
    tmp_path, monkeypatch
):
    """删除项目时应兼容清理历史 slide_revisions 表，避免外键删除失败。"""
    db_path = tmp_path / "project_delete.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")

    class FakeStorage:
        backend_name = "minio"

        def __init__(self):
            self.deleted = []

        async def delete(self, key):
            self.deleted.append(key)

    storage = FakeStorage()
    monkeypatch.setattr(
        "landppt.services.storage.get_artifact_storage", lambda: storage
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE slide_revisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id VARCHAR(36) NOT NULL,
                        revision_note TEXT,
                        FOREIGN KEY (project_id) REFERENCES projects (project_id)
                    )
                    """
                )
            )

        with Session(engine) as sync_session:
            sync_session.add(
                User(
                    id=1,
                    username="tester",
                    password_hash="hash",
                    is_active=True,
                    is_admin=False,
                )
            )
            sync_session.add(
                Project(
                    project_id="project-1",
                    user_id=1,
                    title="测试项目",
                    scenario="demo",
                    topic="删除兼容性",
                )
            )
            sync_session.commit()

            sync_session.execute(
                text(
                    """
                    INSERT INTO slide_revisions (project_id, revision_note)
                    VALUES (:project_id, :revision_note)
                    """
                ),
                {"project_id": "project-1", "revision_note": "legacy revision"},
            )
            sync_session.add(
                AsyncTask(
                    id="task-1",
                    task_type="export_pdf",
                    status="completed",
                    user_id=1,
                    project_id="project-1",
                    progress=1.0,
                    input_data={},
                )
            )
            sync_session.add(
                Artifact(
                    id="artifact-1",
                    user_id=1,
                    project_id="project-1",
                    task_id="task-1",
                    artifact_type="export_pdf",
                    storage_backend="minio",
                    storage_key="exports/project-1/task-1.pdf",
                    filename="project-1.pdf",
                    content_type="application/pdf",
                    size_bytes=123,
                    checksum_sha256="abc123",
                )
            )
            sync_session.commit()

            repo = ProjectRepository(_AsyncSessionAdapter(sync_session))
            success = await repo.delete("project-1", user_id=1)

            assert success is True

            project_count = sync_session.execute(
                text("SELECT COUNT(*) FROM projects WHERE project_id = :project_id"),
                {"project_id": "project-1"},
            ).scalar_one()
            slide_revision_count = sync_session.execute(
                text("SELECT COUNT(*) FROM slide_revisions WHERE project_id = :project_id"),
                {"project_id": "project-1"},
            ).scalar_one()
            artifact_count = sync_session.execute(
                text("SELECT COUNT(*) FROM artifacts WHERE project_id = :project_id"),
                {"project_id": "project-1"},
            ).scalar_one()
            async_task_count = sync_session.execute(
                text("SELECT COUNT(*) FROM async_tasks WHERE project_id = :project_id"),
                {"project_id": "project-1"},
            ).scalar_one()

            assert project_count == 0
            assert slide_revision_count == 0
            assert artifact_count == 0
            assert async_task_count == 0
            assert storage.deleted == ["exports/project-1/task-1.pdf"]
    finally:
        engine.dispose()
