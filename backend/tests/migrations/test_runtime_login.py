"""The migrate step, the boot gate and the runtime login (BH.Loop fork, T-CYK-1009 AC-4, C1).

Production splits the database logins. The owner runs ``scripts/start/migrate.sh``
as its own step; the API, worker and beat log in as a member of
``bh_loop_wearables_runtime``, which may read and write rows and nothing else.

These tests build that split on the test server: a non-superuser owner of its own
database (a superuser would pass every grant check and hide a missing one), a
NOLOGIN runtime role and a login in it, and a NOLOGIN ``cloudsqlsuperuser``
stand-in that is granted nothing. CONNECT is revoked from PUBLIC, as BH.Loop does
for the sidecar database.
"""

import os
import selectors
import signal
import socket
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import LiteralString

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
MIGRATED_DB = "zz_wearables_migrated"
EMPTY_DB = "zz_wearables_empty"


def _alembic_head() -> str | None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


HEAD = _alembic_head()


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
        "ENVIRONMENT": "production",
    }


def _run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", script], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=600)


@pytest.fixture(scope="module")
def admin_url(_postgres_url: str) -> Generator[str, None, None]:
    """The roles and the two databases, created by the test server's superuser."""
    url = make_url(_postgres_url)
    admin = _conninfo(_postgres_url, str(url.username), str(url.password), str(url.database))
    attributes = "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
    with psycopg.connect(admin, autocommit=True) as conn:
        for dbname in (MIGRATED_DB, EMPTY_DB):
            conn.execute(f"DROP DATABASE IF EXISTS {dbname} WITH (FORCE)")
        for role in (RUNTIME_LOGIN, RUNTIME, OWNER, "cloudsqlsuperuser"):
            conn.execute(f"DROP ROLE IF EXISTS {role}")
        conn.execute(f"CREATE ROLE {OWNER} LOGIN PASSWORD '{PASSWORD}' {attributes}")
        conn.execute(f"CREATE ROLE {RUNTIME} NOLOGIN {attributes}")
        conn.execute(f"CREATE ROLE {RUNTIME_LOGIN} LOGIN PASSWORD '{PASSWORD}' {attributes} IN ROLE {RUNTIME}")
        conn.execute("CREATE ROLE cloudsqlsuperuser NOLOGIN")
        for dbname in (MIGRATED_DB, EMPTY_DB):
            conn.execute(f"CREATE DATABASE {dbname} OWNER {OWNER}")
            conn.execute(f"REVOKE CONNECT ON DATABASE {dbname} FROM PUBLIC")
    yield _postgres_url
    with psycopg.connect(admin, autocommit=True) as conn:
        for dbname in (MIGRATED_DB, EMPTY_DB):
            conn.execute(f"DROP DATABASE IF EXISTS {dbname} WITH (FORCE)")
        for role in (RUNTIME_LOGIN, RUNTIME, OWNER, "cloudsqlsuperuser"):
            conn.execute(f"DROP ROLE IF EXISTS {role}")


@pytest.fixture(scope="module")
def migrate_step(admin_url: str) -> subprocess.CompletedProcess[str]:
    """``migrate.sh`` run once, as the owner, on its own database."""
    return _run("scripts/start/migrate.sh", _script_env(admin_url, OWNER, MIGRATED_DB))


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


class TestMigrateStep:
    def test_migrates_and_seeds_as_a_non_superuser_owner(
        self, admin_url: str, migrate_step: subprocess.CompletedProcess[str]
    ) -> None:
        assert migrate_step.returncode == 0, migrate_step.stderr[-2000:]
        with psycopg.connect(_conninfo(admin_url, OWNER, PASSWORD, MIGRATED_DB)) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [(HEAD,)]
            assert conn.execute("SELECT count(*) > 0 FROM provider_settings").fetchall() == [(True,)]
            assert conn.execute("SELECT count(*) > 0 FROM series_type_definition").fetchall() == [(True,)]
            assert conn.execute("SELECT count(*) FROM developer").fetchall() == [(1,)]


class TestBootGate:
    def test_refuses_an_unmigrated_database_instead_of_migrating(self, admin_url: str) -> None:
        env = {**_script_env(admin_url, RUNTIME_LOGIN, EMPTY_DB), "API_PORT": str(_free_port())}

        result = _run("scripts/start/app.sh", env)

        assert result.returncode == 78
        assert "WEARABLES_SCHEMA_BEHIND" in result.stderr
        with psycopg.connect(_conninfo(admin_url, OWNER, PASSWORD, EMPTY_DB)) as conn:
            tables = conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'").fetchall()
        assert tables == [(0,)]

    def test_serves_a_database_at_head(self, admin_url: str, runtime: psycopg.Connection) -> None:
        env = {**_script_env(admin_url, RUNTIME_LOGIN, MIGRATED_DB), "API_PORT": str(_free_port())}
        app = subprocess.Popen(
            ["bash", "scripts/start/app.sh"],
            cwd=BACKEND,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        output: list[str] = []
        try:
            assert app.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(app.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline and app.poll() is None:
                if selector.select(timeout=1):
                    line = app.stdout.readline()
                    output.append(line)
                    if "Starting the FastAPI application" in line:
                        break
        finally:
            os.killpg(app.pid, signal.SIGTERM)
            app.wait(timeout=60)

        assert any("Starting the FastAPI application" in line for line in output), "".join(output)[-2000:]
        assert not any("WEARABLES_SCHEMA_BEHIND" in line for line in output)


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
            "DROP POLICY IF EXISTS any_policy ON user_connection",
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
