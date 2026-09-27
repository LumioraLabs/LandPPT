import builtins
import contextlib
import io
import posixpath
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def production_stage(dockerfile: str) -> str:
    return dockerfile.split(
        "FROM python:3.11-slim-bookworm AS production", 1
    )[1]


def test_runtime_image_uses_fixed_non_root_identity():
    stage = production_stage(read_repo_file("Dockerfile"))

    assert "ARG LANDPPT_UID=10001" in stage
    assert "ARG LANDPPT_GID=10001" in stage
    assert "groupadd --gid \"${LANDPPT_GID}\" landppt" in stage
    assert "useradd --uid \"${LANDPPT_UID}\"" in stage
    assert "HOME=/home/landppt" in stage
    assert "chmod 640 /app/.env" in stage
    assert "chmod 666 /app/.env" not in stage
    assert "\nUSER landppt\n" in stage
    assert stage.rfind("USER landppt") < stage.find("HEALTHCHECK")


def test_entrypoint_checks_identity_and_never_repairs_permissions():
    entrypoint = read_repo_file("docker-entrypoint.sh")

    assert "check_runtime_identity()" in entrypoint
    assert 'if [ "$(id -u)" -eq 0 ]; then' in entrypoint
    assert "LandPPT must not run as root" in entrypoint
    assert "check_env_permissions()" in entrypoint
    assert "Required path is not writable" in entrypoint
    assert "fix_env_permissions" not in entrypoint
    assert "chmod " not in entrypoint
    assert "chown " not in entrypoint
    assert 'cp "/app/.env"' not in entrypoint


def test_entrypoint_preflights_runtime_roots_before_children():
    entrypoint = read_repo_file("docker-entrypoint.sh")
    create_directories = entrypoint.split("create_directories() {", 1)[1].split(
        "\n}", 1
    )[0]

    for root, child in (
        ("/app/temp", "/app/temp/ai_responses_cache"),
        ("/app/lib", "/app/lib/Linux"),
    ):
        assert f'"{root}"' in create_directories
        assert create_directories.index(f'"{root}"') < create_directories.index(
            f'"{child}"'
        )

    assert 'error "Required path cannot be created: $dir"' in create_directories
    assert 'error "Required path is not writable: $dir"' in create_directories


def compose_init_service(compose_text: str) -> str:
    services = compose_text.split("\nservices:\n", 1)[1]
    return services.split("  permissions-init:\n", 1)[1].split(
        "\n  landppt:\n", 1
    )[0]


def compose_service(
    compose_text: str, service_name: str, next_service_name: str
) -> str:
    services = compose_text.split("\nservices:\n", 1)[1]
    return services.split(f"  {service_name}:\n", 1)[1].split(
        f"\n  {next_service_name}:\n", 1
    )[0]


def compose_sequence(service_text: str, key: str, next_key: str) -> list[str]:
    sequence = service_text.split(f"\n    {key}:\n", 1)[1].split(
        f"\n    {next_key}:", 1
    )[0]
    return [
        line.removeprefix("      - ")
        for line in sequence.splitlines()
        if line.startswith("      - ")
    ]


def compose_dependency_anchor(compose_text: str) -> str:
    return compose_text.split(
        "x-landppt-depends-on: &landppt-depends-on", 1
    )[1].split("\n\nservices:", 1)[0]


def embedded_python(script: str, function_name: str) -> str:
    function = script.split(f"{function_name}() {{", 1)[1].split("\n}", 1)[0]
    return function.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


class FakeAccessOS:
    O_WRONLY = 1 << 0
    O_APPEND = 1 << 1
    O_CREAT = 1 << 2
    O_EXCL = 1 << 3
    path = posixpath

    def __init__(self):
        self.effective_uid = 0
        self.effective_gid = 0
        self.supplementary_groups = [0]
        self.files = {}
        self.descriptors = {}
        self.created_paths = []
        self.closed_descriptors = []
        self.unlinked_paths = []
        self.events = []
        self.interrupt_next_close = False
        self.next_descriptor = 10

    def setgroups(self, groups):
        self.supplementary_groups = list(groups)

    def setgid(self, gid):
        self.effective_gid = gid

    def setuid(self, uid):
        self.effective_uid = uid

    def getpid(self):
        return 4242

    def open(self, path, flags, mode=None):
        self.events.append(("open", path))
        if flags & self.O_EXCL and path in self.files:
            raise FileExistsError(path)

        descriptor = self.next_descriptor
        self.next_descriptor += 1
        self.files[path] = (self.effective_uid, self.effective_gid, mode)
        self.descriptors[descriptor] = path
        self.created_paths.append(path)
        return descriptor

    def close(self, descriptor):
        self.events.append(("close", descriptor))
        if self.interrupt_next_close:
            self.interrupt_next_close = False
            raise KeyboardInterrupt("interrupted after probe creation")
        self.closed_descriptors.append(descriptor)

    def unlink(self, path):
        self.events.append(("unlink", path))
        del self.files[path]
        self.unlinked_paths.append(path)

    def restart_with_same_pid(self):
        self.effective_uid = 0
        self.effective_gid = 0
        self.supplementary_groups = [0]
        self.descriptors = {}
        self.next_descriptor = 10


class FakeTempfile:
    def __init__(self, fake_os: FakeAccessOS, suffixes: list[str]):
        self.fake_os = fake_os
        self.suffixes = iter(suffixes)
        self.attempted_paths = []

    def mkstemp(self, *, prefix, dir):
        for suffix in self.suffixes:
            path = posixpath.join(dir, f"{prefix}{suffix}")
            self.attempted_paths.append(path)
            try:
                descriptor = self.fake_os.open(
                    path,
                    self.fake_os.O_WRONLY
                    | self.fake_os.O_CREAT
                    | self.fake_os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                continue
            return descriptor, path
        raise FileExistsError("no unique temporary probe name")


def run_embedded_access_python(
    code: str, fake_os: FakeAccessOS, fake_tempfile: FakeTempfile
):
    fake_sys = SimpleNamespace(
        argv=["access-test", "10001", "10001", "/volume", "directory"]
    )
    real_import = builtins.__import__

    def import_for_access(name, *args, **kwargs):
        if name == "os":
            return fake_os
        if name == "sys":
            return fake_sys
        if name == "tempfile":
            return fake_tempfile
        return real_import(name, *args, **kwargs)

    namespace = {
        "__builtins__": {
            **vars(builtins),
            "__import__": import_for_access,
        }
    }
    exec(compile(code, "docker-permissions-init.sh", "exec"), namespace)


class FakeMarkerOS:
    O_PATH = 1 << 0
    O_CLOEXEC = 1 << 1
    O_NOFOLLOW = 1 << 2
    O_WRONLY = 1 << 3
    O_CREAT = 1 << 4
    O_EXCL = 1 << 5

    def __init__(self, marker_stat=None, open_error=None, fstat_error=None):
        self.marker_stat = marker_stat
        self.open_error = open_error
        self.fstat_error = fstat_error
        self.open_flags = None
        self.closed = False
        self.effective_uid = 0
        self.effective_gid = 0
        self.supplementary_groups = [0]
        self.current_umask = 0o022
        self.events = []

    def open(self, path, flags, mode=None):
        self.open_flags = flags
        self.events.append(
            (
                "open",
                path,
                flags,
                mode,
                self.effective_uid,
                self.effective_gid,
                self.current_umask,
            )
        )
        if self.open_error is not None:
            raise self.open_error
        if flags & self.O_CREAT:
            self.marker_stat = SimpleNamespace(
                st_mode=stat.S_IFREG | (mode & ~self.current_umask),
                st_uid=self.effective_uid,
                st_gid=self.effective_gid,
            )
        return 7

    def fstat(self, _descriptor):
        if self.fstat_error is not None:
            error = self.fstat_error
            self.fstat_error = None
            raise error
        return self.marker_stat

    def close(self, _descriptor):
        self.closed = True

    def setgroups(self, groups):
        self.events.append(("setgroups", tuple(groups)))
        self.supplementary_groups = list(groups)

    def setgid(self, gid):
        self.events.append(("setgid", gid))
        self.effective_gid = gid

    def setuid(self, uid):
        self.events.append(("setuid", uid))
        self.effective_uid = uid

    def umask(self, mode):
        self.events.append(("umask", mode))
        previous = self.current_umask
        self.current_umask = mode
        return previous

    def fchown(self, _descriptor, uid, gid):
        self.events.append(("fchown", uid, gid))
        self.marker_stat.st_uid = uid
        self.marker_stat.st_gid = gid

    def fchmod(self, _descriptor, mode):
        self.events.append(("fchmod", mode))
        file_type = stat.S_IFMT(self.marker_stat.st_mode)
        self.marker_stat.st_mode = file_type | mode


def run_embedded_marker_python(code: str, fake_os: FakeMarkerOS):
    fake_sys = SimpleNamespace(argv=["marker-test", "10001", "10001", "/marker"])
    real_import = builtins.__import__

    def import_for_marker(name, *args, **kwargs):
        if name == "os":
            return fake_os
        if name == "sys":
            return fake_sys
        return real_import(name, *args, **kwargs)

    namespace = {
        "__builtins__": {
            **vars(builtins),
            "__import__": import_for_marker,
        }
    }
    output = io.StringIO()
    exit_code = None
    with contextlib.redirect_stdout(output):
        try:
            exec(compile(code, "docker-permissions-init.sh", "exec"), namespace)
        except SystemExit as error:
            exit_code = error.code
    return output.getvalue().strip(), exit_code


@pytest.mark.parametrize("compose_path", ["docker-compose.yml", "docker-compose-dev.yaml"])
def test_compose_migrates_permissions_before_app_start(compose_path: str):
    compose_text = read_repo_file(compose_path)
    init_service = compose_init_service(compose_text)
    dependencies = compose_dependency_anchor(compose_text)
    landppt_service = compose_service(compose_text, "landppt", "worker")
    worker_service = compose_service(compose_text, "worker", "postgres")

    assert 'user: "0:0"' in init_service
    assert 'entrypoint: ["/usr/local/bin/docker-permissions-init.sh"]' in init_service
    assert 'network_mode: "none"' in init_service
    assert "read_only: true" in init_service
    assert 'restart: "no"' in init_service
    assert compose_sequence(init_service, "cap_drop", "cap_add") == ["ALL"]
    assert compose_sequence(init_service, "cap_add", "security_opt") == [
        "CHOWN",
        "FOWNER",
        "DAC_OVERRIDE",
        "SETUID",
        "SETGID",
    ]
    assert "no-new-privileges:true" in init_service

    assert compose_sequence(init_service, "volumes", "network_mode") == [
        "${LANDPPT_ENV_FILE:-./.env}:/mnt/landppt/env/.env",
        "landppt_data:/mnt/landppt/data",
        "landppt_uploads:/mnt/landppt/uploads",
        "landppt_reports:/mnt/landppt/reports",
        "landppt_cache:/mnt/landppt/cache",
        "landppt_lib:/mnt/landppt/lib",
    ]

    assert "/app" not in init_service
    assert "docker.sock" not in init_service
    assert "/var/run/docker.sock" not in init_service
    assert "${LANDPPT_ENV_FILE:-./.env}:/app/.env" in compose_text
    assert "permissions-init:" in dependencies
    permissions_dependency = dependencies.split("  permissions-init:\n", 1)[1].split(
        "\n  postgres:", 1
    )[0]
    assert "condition: service_completed_successfully" in permissions_dependency
    assert "depends_on: *landppt-depends-on" in landppt_service
    assert "depends_on: *landppt-depends-on" in worker_service


def test_permission_migration_script_is_idempotent_and_validates_as_target_user():
    script = read_repo_file("docker-permissions-init.sh")
    dockerfile = read_repo_file("Dockerfile")

    assert ".landppt-permissions-v1" in script
    assert 'LANDPPT_UID:-10001' in script
    assert 'LANDPPT_GID:-10001' in script
    assert "os.setgid(gid)" in script
    assert "os.setuid(uid)" in script
    assert 'chown -R "${TARGET_UID}:${TARGET_GID}"' in script
    assert "chmod -R u+rwX" in script
    assert "docker-permissions-init.sh /usr/local/bin/" in dockerfile


def test_directory_access_probe_survives_interruption_and_reused_pid():
    script = read_repo_file("docker-permissions-init.sh")
    validate_access = embedded_python(script, "validate_access")
    fake_os = FakeAccessOS()

    fake_os.interrupt_next_close = True
    first_tempfile = FakeTempfile(fake_os, ["same"])
    with pytest.raises(KeyboardInterrupt, match="interrupted after probe creation"):
        run_embedded_access_python(validate_access, fake_os, first_tempfile)

    assert len(fake_os.files) == 1
    stale_probe = next(iter(fake_os.files))
    assert fake_os.files[stale_probe][:2] == (10001, 10001)

    fake_os.restart_with_same_pid()
    restart_tempfile = FakeTempfile(fake_os, ["same", "next"])
    run_embedded_access_python(validate_access, fake_os, restart_tempfile)

    assert restart_tempfile.attempted_paths == [
        stale_probe,
        "/volume/.landppt-write-test-next",
    ]
    assert fake_os.created_paths == [
        stale_probe,
        "/volume/.landppt-write-test-next",
    ]
    assert fake_os.files == {stale_probe: (10001, 10001, 0o600)}
    assert fake_os.closed_descriptors == [10]
    assert fake_os.unlinked_paths == ["/volume/.landppt-write-test-next"]
    assert fake_os.events[-2:] == [
        ("close", 10),
        ("unlink", "/volume/.landppt-write-test-next"),
    ]

    failing_os = FakeAccessOS()
    with pytest.raises(FileExistsError, match="no unique temporary probe name"):
        run_embedded_access_python(
            validate_access,
            failing_os,
            FakeTempfile(failing_os, []),
        )
    assert failing_os.files == {}


def test_permission_migration_marker_is_safe_and_keeps_fast_path():
    script = read_repo_file("docker-permissions-init.sh")

    marker_state = embedded_python(script, "marker_state")
    create_marker = embedded_python(script, "create_marker")

    missing_os = FakeMarkerOS(open_error=FileNotFoundError("missing"))
    assert run_embedded_marker_python(marker_state, missing_os) == ("missing", 0)

    valid_os = FakeMarkerOS(
        SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=10001, st_gid=10001)
    )
    assert run_embedded_marker_python(marker_state, valid_os) == ("valid", None)
    assert valid_os.closed is True

    invalid_markers = [
        (stat.S_IFLNK | 0o777, 10001, 10001, "not a regular file"),
        (stat.S_IFIFO | 0o600, 10001, 10001, "not a regular file"),
        (stat.S_IFREG | 0o600, 0, 0, "unexpected owner"),
        (stat.S_IFREG | 0o644, 10001, 10001, "unexpected mode"),
    ]
    for marker_mode, marker_uid, marker_gid, expected_error in invalid_markers:
        fake_os = FakeMarkerOS(
            SimpleNamespace(
                st_mode=marker_mode,
                st_uid=marker_uid,
                st_gid=marker_gid,
            )
        )
        _output, exit_code = run_embedded_marker_python(marker_state, fake_os)
        assert expected_error in str(exit_code)

    existing_os = FakeMarkerOS(open_error=FileExistsError("already exists"))
    _output, exit_code = run_embedded_marker_python(create_marker, existing_os)
    assert "Could not create migration marker" in str(exit_code)

    created_os = FakeMarkerOS()
    assert run_embedded_marker_python(create_marker, created_os) == ("", None)
    assert created_os.open_flags & created_os.O_EXCL
    assert created_os.open_flags & created_os.O_NOFOLLOW
    assert [event[0] for event in created_os.events[:5]] == [
        "setgroups",
        "setgid",
        "setuid",
        "umask",
        "open",
    ]
    assert created_os.events[0] == ("setgroups", ())
    assert created_os.events[4][3:] == (0o600, 10001, 10001, 0o077)
    assert created_os.marker_stat.st_uid == 10001
    assert created_os.marker_stat.st_gid == 10001
    assert stat.S_IMODE(created_os.marker_stat.st_mode) == 0o600
    assert created_os.closed is True

    interrupted_os = FakeMarkerOS(
        fstat_error=KeyboardInterrupt("interrupted after marker creation")
    )
    with pytest.raises(KeyboardInterrupt, match="interrupted after marker creation"):
        run_embedded_marker_python(create_marker, interrupted_os)
    assert interrupted_os.closed is True
    assert run_embedded_marker_python(marker_state, interrupted_os) == ("valid", None)

    assert "marker_state()" in script
    assert "create_marker()" in script
    assert "os.O_PATH" in script
    assert "os.O_NOFOLLOW" in script
    assert "os.O_EXCL" in script
    assert "os.fstat(" in script
    assert "stat.S_ISREG(" in script
    assert ".st_uid != uid" in script
    assert ".st_gid != gid" in script
    assert "stat.S_IMODE(" in script
    assert "!= 0o600" in script
    assert "os.fchown(" not in script
    assert "os.fchmod(" not in script
    assert '[ -f "$marker_path" ]' not in script
    assert ': > "$marker_path"' not in script
    assert script.count('chown -R "${TARGET_UID}:${TARGET_GID}"') == 1
    assert "Migration marker found for ${volume_path}; validating" in script


def test_helm_defaults_enforce_non_root_identity_and_volume_group():
    values = read_repo_file("helm/landppt/values.yaml")
    pod_context = values.split("podSecurityContext:", 1)[1].split(
        "\nsecurityContext:", 1
    )[0]
    container_context = values.split("\nsecurityContext:", 1)[1].split(
        "\nlivenessProbe:", 1
    )[0]

    for expected in (
        "fsGroup: 10001",
        "fsGroupChangePolicy: OnRootMismatch",
        "type: RuntimeDefault",
    ):
        assert expected in pod_context
    assert "runAsUser" not in pod_context

    volume_permissions = values.split("volumePermissions:", 1)[1].split(
        "\nresources:", 1
    )[0]
    for expected in (
        "enabled: true",
        "uid: 10001",
        "gid: 10001",
    ):
        assert expected in volume_permissions

    for expected in (
        "runAsNonRoot: true",
        "runAsUser: 10001",
        "runAsGroup: 10001",
        "allowPrivilegeEscalation: false",
        "- ALL",
    ):
        assert expected in container_context


@pytest.mark.parametrize(
    "template_path",
    [
        "helm/landppt/templates/deployment.yaml",
        "helm/landppt/templates/worker-deployment.yaml",
    ],
)
def test_helm_workloads_render_pod_and_container_security_contexts(
    template_path: str,
):
    template = read_repo_file(template_path)

    assert "{{- with .Values.podSecurityContext }}" in template
    assert "{{- with .Values.securityContext }}" in template


def test_migrate_init_container_renders_container_security_context():
    helper = read_repo_file("helm/landppt/templates/_helpers.tpl")
    migrate = helper.split('define "landppt.migrateInitContainer"', 1)[1]
    assert "{{- with .Values.securityContext }}" in migrate


def test_helm_web_update_strategy_supports_zero_unavailable_rollouts():
    values = read_repo_file("helm/landppt/values.yaml")
    template = read_repo_file("helm/landppt/templates/deployment.yaml")

    assert 'type: ""' in values
    assert "maxUnavailable: 0" in values
    assert "maxSurge: 1" in values
    assert '$strategyType := .Values.web.updateStrategy.type | default' in template
    assert 'eq $strategyType "RollingUpdate"' in template
    assert ".Values.web.updateStrategy.rollingUpdate.maxUnavailable" in template
    assert ".Values.web.updateStrategy.rollingUpdate.maxSurge" in template


def test_helm_web_repairs_persistent_volume_permissions_before_startup():
    template = read_repo_file("helm/landppt/templates/deployment.yaml")
    init_container = template.split("- name: volume-permissions", 1)[1].split(
        "{{- if .Values.minio.enabled }}", 1
    )[0]

    assert ".Values.persistence.enabled .Values.volumePermissions.enabled" in template
    assert "runAsUser: 0" in init_container
    assert "allowPrivilegeEscalation: false" in init_container
    assert "readOnlyRootFilesystem: true" in init_container
    for capability in ("CHOWN", "DAC_OVERRIDE", "FOWNER"):
        assert f"- {capability}" in init_container
    for path in (
        "/app/data",
        "/app/uploads",
        "/app/research_reports",
        "/app/temp",
        "/app/lib",
    ):
        assert path in init_container
    assert "chown -R {{ .Values.volumePermissions.uid }}:" in init_container
    assert 'chmod -R u+rwX "$path"' in init_container
