"""Migrate step, boot gate, runtime login and served-API logs (BH.Loop fork, T-CYK-1009 AC-4, AC-5, C1).

Production splits the database logins. The owner runs ``scripts/start/migrate.sh``
as its own step; the API, worker and beat log in as a member of
``bh_loop_wearables_runtime``, which may read and write rows and nothing else.

These tests build that split on the test server: a non-superuser owner of its own
database (a superuser would pass every grant check and hide a missing one), a
NOLOGIN runtime role and a login in it, and a NOLOGIN ``cloudsqlsuperuser``
stand-in that is granted nothing. CONNECT is revoked from PUBLIC, as BH.Loop does
for the sidecar database. ``TestServedApiLogs`` runs the real ``fastapi run``
process, whose uvicorn logging config the in-process TestClient never loads.
"""

import json
import os
import signal
import socket
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import LiteralString

import httpx
import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import make_url

BACKEND = Path(__file__).resolve().parents[2]
OWNER = "bh_loop_wearables"
RUNTIME = "bh_loop_wearables_runtime"
RUNTIME_LOGIN = "zz_wearables_runtime_login"
PASSWORD = "synthetic-test-password"
ADMIN_PASSWORD = "synthetic-admin-password"
MIGRATED_DB = "zz_wearables_migrated"
EMPTY_DB = "zz_wearables_empty"
BEHIND_DB = "zz_wearables_behind"
DATABASES = (MIGRATED_DB, EMPTY_DB, BEHIND_DB)
# synthetic probe values the served API must never print
PROBE_USER_ID = "17391700-0000-4000-8000-000000173917"
PROBE_HOOK = Path(__file__).resolve().parent / "served_probe"


def _scripts() -> ScriptDirectory:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "migrations"))
    return ScriptDirectory.from_config(config)


HEAD = _scripts().get_current_head()
BEHIND = _scripts().get_revision(HEAD).down_revision if HEAD else None


def _conninfo(admin_url: str, user: str, password: str, dbname: str) -> str:
    url = make_url(admin_url)
    return f"host={url.host} port={url.port} user={user} password={password} dbname={dbname}"


def _script_env(admin_url: str, user: str, dbname: str) -> dict[str, str]:
    url = make_url(admin_url)
    return {
        **os.environ,
        "DB_HOST": str(url.host),
        "DB_PORT": str(url.port),
        "DB_NAME": dbname,
        "DB_USER": user,
        "DB_PASSWORD": PASSWORD,
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
        "ENVIRONMENT": "production",
    }


def _run(command: list[str], env: dict[str, str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=BACKEND, env=env, capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope="module")
def admin_url(_postgres_url: str) -> Generator[str, None, None]:
    """The roles and the two databases, created by the test server's superuser."""
    url = make_url(_postgres_url)
    admin = _conninfo(_postgres_url, str(url.username), str(url.password), str(url.database))
    attributes = "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
    with psycopg.connect(admin, autocommit=True) as conn:
        for dbname in DATABASES:
            conn.execute(f"DROP DATABASE IF EXISTS {dbname} WITH (FORCE)")
        for role in (RUNTIME_LOGIN, RUNTIME, OWNER, "cloudsqlsuperuser"):
            conn.execute(f"DROP ROLE IF EXISTS {role}")
        conn.execute(f"CREATE ROLE {OWNER} LOGIN PASSWORD '{PASSWORD}' {attributes}")
        conn.execute(f"CREATE ROLE {RUNTIME} NOLOGIN {attributes}")
        conn.execute(f"CREATE ROLE {RUNTIME_LOGIN} LOGIN PASSWORD '{PASSWORD}' {attributes} IN ROLE {RUNTIME}")
        conn.execute("CREATE ROLE cloudsqlsuperuser NOLOGIN")
        for dbname in DATABASES:
            conn.execute(f"CREATE DATABASE {dbname} OWNER {OWNER}")
            conn.execute(f"REVOKE CONNECT ON DATABASE {dbname} FROM PUBLIC")
    yield _postgres_url
    with psycopg.connect(admin, autocommit=True) as conn:
        for dbname in DATABASES:
            conn.execute(f"DROP DATABASE IF EXISTS {dbname} WITH (FORCE)")
        for role in (RUNTIME_LOGIN, RUNTIME, OWNER, "cloudsqlsuperuser"):
            conn.execute(f"DROP ROLE IF EXISTS {role}")


@pytest.fixture(scope="module")
def migrate_step(admin_url: str) -> subprocess.CompletedProcess[str]:
    """``migrate.sh`` run once, as the owner, on its own database."""
    return _run(["bash", "scripts/start/migrate.sh"], _script_env(admin_url, OWNER, MIGRATED_DB))


@pytest.fixture
def runtime(
    admin_url: str, migrate_step: subprocess.CompletedProcess[str]
) -> Generator[psycopg.Connection, None, None]:
    assert migrate_step.returncode == 0, migrate_step.stderr[-2000:]
    with psycopg.connect(_conninfo(admin_url, RUNTIME_LOGIN, PASSWORD, MIGRATED_DB), autocommit=True) as conn:
        yield conn


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _version(admin_url: str, dbname: str) -> list[tuple[str]]:
    with psycopg.connect(_conninfo(admin_url, OWNER, PASSWORD, dbname)) as conn:
        return conn.execute("SELECT version_num FROM alembic_version").fetchall()


@contextmanager
def _served(env: dict[str, str], log: Path) -> Generator[str, None, None]:
    """``app.sh`` serving on a free port, its stdout and stderr in ``log``; yields its base URL."""
    port = _free_port()
    with log.open("w") as out:
        app = subprocess.Popen(
            ["bash", "scripts/start/app.sh"],
            cwd=BACKEND,
            env={**env, "API_PORT": str(port)},
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 300
        while app.poll() is None and time.monotonic() < deadline:
            with suppress(httpx.TransportError):
                if httpx.get(f"{base}/").status_code == 200:
                    break
            time.sleep(0.5)
        else:
            raise AssertionError(f"app.sh did not serve: {log.read_text()[-2000:]}")
        yield base
    finally:
        os.killpg(app.pid, signal.SIGTERM)
        app.wait(timeout=60)


class TestBootGate:
    def test_refuses_an_unmigrated_database_instead_of_migrating(self, admin_url: str) -> None:
        env = {**_script_env(admin_url, RUNTIME_LOGIN, EMPTY_DB), "API_PORT": str(_free_port())}

        result = _run(["bash", "scripts/start/app.sh"], env, timeout=120)

        assert result.returncode == 78
        assert "WEARABLES_SCHEMA_BEHIND" in result.stderr
        with psycopg.connect(_conninfo(admin_url, OWNER, PASSWORD, EMPTY_DB)) as conn:
            tables = conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'").fetchall()
        assert tables == [(0,)]

    def test_refuses_a_database_one_revision_behind(self, admin_url: str) -> None:
        # as the owner, who could migrate: the gate refuses for the schema, not for a missing grant
        env = _script_env(admin_url, OWNER, BEHIND_DB)
        behind = _run(["uv", "run", "alembic", "upgrade", str(BEHIND)], env)
        assert behind.returncode == 0, behind.stderr[-2000:]

        result = _run(["bash", "scripts/start/app.sh"], {**env, "API_PORT": str(_free_port())}, timeout=120)

        assert result.returncode == 78
        assert "WEARABLES_SCHEMA_BEHIND" in result.stderr
        assert _version(admin_url, BEHIND_DB) == [(BEHIND,)]

    def test_serves_a_database_at_head(self, admin_url: str, runtime: psycopg.Connection, tmp_path: Path) -> None:
        env = _script_env(admin_url, RUNTIME_LOGIN, MIGRATED_DB)

        with _served(env, tmp_path / "app.log") as base:
            assert httpx.get(f"{base}/").status_code == 200

        assert "WEARABLES_SCHEMA_BEHIND" not in (tmp_path / "app.log").read_text()


@pytest.fixture(scope="module")
def served_output(
    admin_url: str, migrate_step: subprocess.CompletedProcess[str], tmp_path_factory: pytest.TempPathFactory
) -> list[str]:
    """Every line the served API printed after one unhandled 500 and the ``served_probe`` hook's emits."""
    assert migrate_step.returncode == 0, migrate_step.stderr[-2000:]
    folder = tmp_path_factory.mktemp("served")
    trigger = folder / "emit"
    env = {
        **_script_env(admin_url, RUNTIME_LOGIN, MIGRATED_DB),
        "PYTHONPATH": str(PROBE_HOOK),
        "SERVED_PROBE_TRIGGER": str(trigger),
    }
    with _served(env, folder / "app.log") as base:
        login = httpx.post(
            f"{base}/api/v1/auth/login", data={"username": "admin@admin.com", "password": ADMIN_PASSWORD}
        )
        assert login.status_code == 200
        bearer = {"Authorization": f"Bearer {login.json()['access_token']}"}
        # no such user: the refresh-token insert breaks its foreign key, an unhandled 500
        token = httpx.post(f"{base}/api/v1/users/{PROBE_USER_ID}/token", headers=bearer, json={})
        assert token.status_code == 500
        trigger.touch()
        deadline = time.monotonic() + 60
        while not (folder / "emit.done").exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert (folder / "emit.done").exists(), "the served_probe hook did not run"
    return (folder / "app.log").read_text().splitlines()


def _records(lines: list[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in lines if line.startswith("{")]


class TestServedApiLogs:
    """The real ``fastapi run`` process: uvicorn's own logging config, not the in-process TestClient."""

    def test_an_unhandled_500_prints_no_request_value(self, served_output: list[str]) -> None:
        leaks = [line for line in served_output if PROBE_USER_ID in line]
        assert leaks == [], f"{len(leaks)} line(s) carry the request's user id: {leaks[:3]}"
        assert any(record.get("error_type") == "IntegrityError" for record in _records(served_output))

    def test_uvicorns_exception_report_prints_no_reading(self, served_output: list[str]) -> None:
        leaks = [line for line in served_output if "173.917" in line]
        assert leaks == [], f"{len(leaks)} line(s) carry the reading: {leaks[:3]}"
        assert any(
            record.get("logger") == "uvicorn.error" and record.get("error_type") == "ValueError"
            for record in _records(served_output)
        )

    def test_a_python_warning_prints_no_reading(self, served_output: list[str]) -> None:
        leaks = [line for line in served_output if "164.281" in line]
        assert leaks == [], f"{len(leaks)} line(s) carry the reading: {leaks[:3]}"
        assert any(record.get("logger") == "py.warnings" for record in _records(served_output))


class TestRuntimeLogin:
    def test_holds_exactly_row_access(self, runtime: psycopg.Connection) -> None:
        granted = set(
            runtime.execute(
                """
                SELECT c.relname, c.relkind, a.privilege_type
                FROM pg_class c, aclexplode(c.relacl) a
                WHERE c.relnamespace = 'public'::regnamespace AND a.grantee = %s::regrole
                """,
                (RUNTIME,),
            ).fetchall()
        )
        tables = [row[0] for row in runtime.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
        sequences = [
            row[0] for row in runtime.execute("SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")
        ]
        expected = {
            (table, "r", privilege)
            for table in tables
            if table != "alembic_version"
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
        expected |= {("alembic_version", "r", "SELECT")}
        expected |= {(sequence, "S", privilege) for sequence in sequences for privilege in ("USAGE", "SELECT")}
        assert len(tables) > 1
        assert granted == expected

        database = runtime.execute(
            """
            SELECT a.privilege_type FROM pg_database d, aclexplode(d.datacl) a
            WHERE d.datname = current_database() AND a.grantee = %s::regrole
            """,
            (RUNTIME,),
        ).fetchall()
        assert database == [("CONNECT",)]

    @pytest.mark.parametrize(
        "statement",
        [
            "TRUNCATE user_connection",
            "ALTER TABLE user_connection NO FORCE ROW LEVEL SECURITY",
            "CREATE POLICY any_policy ON user_connection USING (true)",
            "DELETE FROM alembic_version",
            "UPDATE alembic_version SET version_num = version_num",
        ],
    )
    def test_is_refused_everything_else(self, runtime: psycopg.Connection, statement: LiteralString) -> None:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime.execute(statement)

    def test_is_no_superuser_owner_or_provider_admin(self, runtime: psycopg.Connection) -> None:
        row = runtime.execute(
            """
            SELECT r.rolsuper, r.rolbypassrls,
                   pg_has_role(current_user, %s, 'MEMBER'),
                   pg_has_role(current_user, 'cloudsqlsuperuser', 'MEMBER')
            FROM pg_roles r WHERE r.rolname = current_user
            """,
            (OWNER,),
        ).fetchone()
        assert row == (False, False, False, False)
