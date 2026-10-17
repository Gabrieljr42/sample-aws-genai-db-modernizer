"""ADR-004 Decision 5: the Assessments API never stores or forwards a submitted secret (#723).

Database credentials live in a secrets manager and the request names them by reference
(`connection.secret_arn` or `connection.secret_name`). The forwarded connection fields are
allowlisted (host name or IP, a plain database name, one of two credential types), and a
request that carries a password, secret, token or a credential-shaped string anywhere else
-- at any depth, under any spelling, percent-, base64- or JSON-encoded -- is refused with a
400 that tells the user to store the credential in a secrets manager and pass a reference.
The refused value is written nowhere: no `_meta.json`, no artifact, no log line, no
execution input. Inputs stored before the fix are scrubbed. Every check is bounded.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import src.api.main as api_main
from src.api import secret_guard, stored_input_redaction
from src.api.main import app
from src.api.models.requests import CollectionOptions, ConnectionDetails
from src.api.routes import agent_interaction, assessments, settings
from src.api.secret_guard import (
    MAX_GUARDED_BODY_BYTES,
    MAX_SCANNED_CHARS,
    RedactSecretQueryFilter,
    find_submitted_secrets,
    is_secret_key,
)
from src.api.services.local_execution import LocalExecutionService
from src.api.stored_input_redaction import (
    MARKER,
    redact_stored_inputs,
    redact_stored_inputs_once,
)
from src.storage.local_store import LocalArtifactStore
from tests.unit.tools.sql._timing import assert_cost_ratio, assert_linear

client = TestClient(app)

# Assembled at runtime so no literal credential sits in the source.
SECRET = "Zq7" + "-not-a-real-" + "value-4Kx9"  # pragma: allowlist secret
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/db/creds-AbCdEf"  # pragma: allowlist secret
SECRET_NAME = "prod/db/credentials"  # pragma: allowlist secret
URI = "mysql://app:" + SECRET + "@db.example.internal:3306/app"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _base(**connection: object) -> dict:
    return {
        "source_database_type": "mysql",
        "database_name": "test_db",
        "connection": {
            "host": "db.example.internal",
            "port": 3306,
            "database": "app",
            **connection,
        },
    }


def _with_options(**options: object) -> dict:
    return {**_base(secret_arn=SECRET_ARN), "options": options}


# (case id, request body). Every body carries SECRET somewhere, in some form.
SECRET_BODIES: list[tuple[str, dict]] = [
    # secret-named keys
    ("password", _base(password=SECRET)),
    ("direct-credentials", _base(credentials_type="direct", username="app", password=SECRET)),
    ("password-beside-a-reference", _base(secret_arn=SECRET_ARN, password=SECRET)),
    ("Password", _base(Password=SECRET)),
    ("PASSWORD", _base(PASSWORD=SECRET)),
    ("passwd", _base(passwd=SECRET)),
    ("pwd", _base(pwd=SECRET)),
    ("db_password", _base(db_password=SECRET)),
    ("dbPassword", _base(dbPassword=SECRET)),
    ("db-password", _base(**{"db-password": SECRET})),
    ("PGPASSWORD-key", _base(PGPASSWORD=SECRET)),
    ("MYSQL_PWD-key", _base(MYSQL_PWD=SECRET)),
    ("passphrase", _base(passphrase=SECRET)),
    ("secret", _base(secret=SECRET)),
    ("client_secret", _base(client_secret=SECRET)),
    ("token", _base(token=SECRET)),
    ("auth_token", _base(auth_token=SECRET)),
    ("api_key", _base(api_key=SECRET)),
    ("secret_access_key", _base(aws_secret_access_key=SECRET)),
    ("private_key", _base(private_key=SECRET)),
    ("numeric-password", _base(password=12345678)),
    ("credentials-object", _base(credentials={"username": "app", "password": SECRET})),
    ("nested-in-connection", _base(auth={"method": "basic", "password": SECRET})),
    ("nested-in-a-list", _base(replicas=[{"host": "r1", "password": SECRET}])),
    ("top-level", {**_base(secret_arn=SECRET_ARN), "password": SECRET}),
    ("in-options", _with_options(password=SECRET)),
    # credential-shaped strings, in the host and in free-text fields
    ("uri-in-host", _base(host=URI)),
    ("uri-in-connection-string", _base(connection_string=URI)),
    ("uri-in-options", _with_options(include_table_patterns=URI)),
    ("postgres-uri", _base(dsn="postgresql://app:" + SECRET + "@db:5432/app")),
    ("uri-password-with-slash-and-hash", _base(dsn="postgres://u:" + SECRET + "/x#y@db/app")),
    ("userinfo-without-scheme", _base(host="admin:" + SECRET + "@db.example.com")),
    (
        "userinfo-without-scheme-in-options",
        _with_options(include_table_patterns="admin:" + SECRET + "@db"),
    ),
    ("oracle-easy-connect", _base(connection_string="scott/" + SECRET + "@db:1521/orcl")),
    ("oracle-jdbc-thin", _base(host="jdbc:oracle:thin:scott/" + SECRET + "@db:1521:orcl")),
    ("jdbc-query", _base(host="jdbc:mysql://db:3306/app?user=app&password=" + SECRET)),
    ("PGPASSWORD-assignment", _base(host="db PGPASSWORD=" + SECRET)),
    ("MYSQL_PWD-assignment", _with_options(exclude_table_patterns="MYSQL_PWD=" + SECRET)),
    ("db_password-assignment", _with_options(include_table_patterns="db_password=" + SECRET)),
    ("mysql-dash-p", _with_options(include_table_patterns="mysql -h db -u app -p" + SECRET)),
    ("colon-form", _with_options(include_table_patterns="password: " + SECRET)),
    ("ado-key-value", _base(connection_string="Server=db;User Id=app;Password=" + SECRET + ";")),
    ("ado-pwd", _base(connection_string="Server=db;Uid=app;Pwd=" + SECRET)),
    ("libpq-key-value", _base(dsn="host=db user=app password=" + SECRET + " dbname=app")),
    # encoded forms
    ("percent-encoded-uri", _base(host=quote(URI, safe=""))),
    ("double-percent-encoded-uri", _base(host=quote(quote(URI, safe=""), safe=""))),
    ("percent-encoded-password-in-uri", _base(host="mysql://app:" + quote(SECRET) + "%40x@db")),
    ("base64-uri", _base(host=_b64(URI))),
    ("base64-key-value", _base(connection_string=_b64("Server=db;Password=" + SECRET))),
    ("json-string", _base(connection_string=json.dumps({"user": "app", "password": SECRET}))),
]

CASE_IDS = [case for case, _ in SECRET_BODIES]
BODIES = [body for _, body in SECRET_BODIES]


def _encoded_forms(value: str) -> list[str]:
    return [value, quote(value, safe=""), _b64(value), json.dumps(value)[1:-1]]


@pytest.fixture
def real_store(tmp_path, monkeypatch):
    """The real filesystem-backed execution service, so a write anywhere under it shows up."""
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    sfn = LocalExecutionService(store)
    monkeypatch.setattr(assessments, "sfn_service", sfn)
    s3 = MagicMock()
    s3.client.generate_presigned_url.return_value = "file:///upload"
    s3.bucket = "local"
    monkeypatch.setattr(assessments, "s3_service", s3)
    return tmp_path / "artifacts"


def _files_containing(root: Path, needles: list[str]) -> list[str]:
    if not root.exists():
        return []
    hits = []
    for path in root.rglob("*"):
        if path.is_file():
            text = path.read_bytes().decode("utf-8", errors="replace")
            if any(n in text for n in needles):
                hits.append(str(path.relative_to(root)))
    return hits


def _meta(root: Path, job_id: str) -> dict:
    meta: dict = json.loads((root / "test_db" / job_id / "_meta.json").read_text())
    return meta


class TestEachSecretFieldIsRefused:
    @pytest.mark.parametrize("body", BODIES, ids=CASE_IDS)
    def test_refused_with_a_400_and_a_secrets_manager_pointer(self, body, real_store) -> None:
        resp = client.post("/api/v1/assessments", json=body)

        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert isinstance(detail, str)
        assert "Secrets Manager" in detail
        assert "secret_arn" in detail and "secret_name" in detail
        assert not any(form in resp.text for form in _encoded_forms(SECRET))

    def test_a_unicode_escaped_key_is_still_refused(self, real_store) -> None:
        raw = json.dumps(_base(secret_arn=SECRET_ARN)).replace(
            '"secret_arn"', '"pass\\u0077ord": "' + SECRET + '", "secret_arn"'
        )
        resp = client.post(
            "/api/v1/assessments", content=raw, headers={"content-type": "application/json"}
        )
        assert resp.status_code == 400, resp.text

    def test_a_text_plain_body_is_checked_too(self, real_store) -> None:
        resp = client.post(
            "/api/v1/assessments",
            content=json.dumps(_base(password=SECRET)),
            headers={"content-type": "text/plain"},
        )
        assert resp.status_code == 400, resp.text

    def test_the_400_comes_before_any_other_validation_error(self, real_store) -> None:
        body = _base(password=SECRET)
        body["connection"]["port"] = 99999  # also invalid: still the secret wins
        resp = client.post("/api/v1/assessments", json=body)
        assert resp.status_code == 400, resp.text
        assert SECRET not in resp.text

    def test_the_message_names_the_field_but_never_the_value(self, real_store) -> None:
        resp = client.post("/api/v1/assessments", json=_base(password=SECRET))
        detail = resp.json()["detail"]
        assert "connection.password" in detail
        assert SECRET not in detail

    def test_query_parameters_are_checked_too(self, real_store) -> None:
        resp = client.post(
            "/api/v1/assessments", params={"password": SECRET}, json=_base(secret_arn=SECRET_ARN)
        )
        assert resp.status_code == 400, resp.text

    @pytest.mark.parametrize("kind", ["direct", "Direct", " DIRECT "])
    def test_direct_credentials_are_refused_even_without_a_password(self, kind, real_store) -> None:
        resp = client.post("/api/v1/assessments", json=_base(credentials_type=kind, username="app"))
        assert resp.status_code == 400, resp.text
        assert "connection.credentials_type=direct" in resp.json()["detail"]

    def test_prepare_refuses_a_secret(self, real_store) -> None:
        resp = client.post(
            "/api/v1/assessments/prepare", json={"database_name": "test_db", "password": SECRET}
        )
        assert resp.status_code == 400, resp.text
        assert SECRET not in resp.text

    def test_prepare_accepts_an_ordinary_request(self, real_store) -> None:
        resp = client.post("/api/v1/assessments/prepare", json={"database_name": "test_db"})
        assert resp.status_code == 201, resp.text

    def test_the_settings_endpoint_refuses_a_secret_and_keeps_none(self) -> None:
        before = json.dumps(settings._settings)
        resp = client.put(
            "/api/v1/settings", json={"aws_configuration": {"aws_secret_access_key": SECRET}}
        )
        assert resp.status_code == 400, resp.text
        assert json.dumps(settings._settings) == before

    @pytest.mark.parametrize(
        "key",
        [
            "max_tokens",
            "token_budget",
            "secret_rotation_days",
            "use_secrets_manager",
            "credentials_note",
            "api_key_header",
        ],
    )
    def test_settings_keys_that_merely_mention_a_secret_are_accepted(self, key) -> None:
        before = dict(settings._settings)
        try:
            resp = client.put("/api/v1/settings", json={"llm": {key: 4096}})
            assert resp.status_code == 200, resp.text
        finally:
            settings._settings.clear()
            settings._settings.update(before)

    def test_a_value_error_from_start_execution_is_a_400(self, monkeypatch) -> None:
        sfn = MagicMock()
        sfn.start_execution.side_effect = ValueError("refusing to store a secret")
        monkeypatch.setattr(assessments, "sfn_service", sfn)
        monkeypatch.setattr(assessments, "s3_service", MagicMock())
        resp = client.post("/api/v1/assessments", json=_base(secret_arn=SECRET_ARN))
        assert resp.status_code == 400, resp.text


class TestTheAgentAnswersAreChecked:
    @pytest.fixture
    def answers_store(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_interaction, "artifact_store", LocalArtifactStore(str(tmp_path)))
        return tmp_path

    URL = "/api/v1/assessments/job-1/agents/schema-dynamodb/{}?database_name=test_db"

    @pytest.mark.parametrize(
        "answer",
        [
            URI,
            "user=app password=" + SECRET,
            "Password = '" + SECRET + "'",
            "admin:" + SECRET + "@db",
            "app:" + SECRET + "@db.example.com:5432",
            "mysql -h db --password " + SECRET,
            "CREATE ROLE app WITH LOGIN PASSWORD '" + SECRET + "'",
            "ALTER ROLE app PASSWORD '" + SECRET + "'",
            "mongosh -u app -p " + SECRET,
            "redis-cli -h cache -a " + SECRET,
        ],
        ids=[
            "uri",
            "assignment",
            "spaced-quoted-assignment",
            "userinfo-bare-host",
            "userinfo-host-port",
            "cli-flag",
            "create-role",
            "alter-role",
            "mongosh",
            "redis-cli",
        ],
    )
    def test_a_credential_shaped_answer_is_refused_and_not_stored(
        self, answer, answers_store
    ) -> None:
        put = client.put(self.URL.format("answers"), json={"answers": {"q1": answer}})
        post = client.post(
            self.URL.format("validate-answer"), json={"question_id": "q1", "answer": answer}
        )
        assert (put.status_code, post.status_code) == (400, 400), (put.text, post.text)
        assert _files_containing(answers_store, [SECRET]) == []

    def test_question_ids_are_data_not_secret_names(self, answers_store) -> None:
        resp = client.put(
            self.URL.format("answers"), json={"answers": {"session_token_ttl": "30 minutes"}}
        )
        assert resp.status_code == 404, resp.text  # no questions artifact in this test

    @pytest.mark.parametrize(
        "answer",
        [
            "users.password: bcrypt hash, never read by the app",
            "token: JWT, expires after 1 hour",
            "token: rotates daily",
            "secret: rotated by the platform team",
            "credentials: stored in Secrets Manager",
            "password: hashed",
            "pwd: /var/lib/mysql",
            "Owner:dba@example.com",
            "pin to @scope/pkg@1.2.3",
            "the batch runs at 10:30@UTC",
        ],
    )
    def test_an_answer_that_describes_a_column_or_mechanism_is_accepted(
        self, answer, answers_store
    ) -> None:
        put = client.put(self.URL.format("answers"), json={"answers": {"q1": answer}})
        post = client.post(
            self.URL.format("validate-answer"), json={"question_id": "q1", "answer": answer}
        )
        # 404: this test has no questions artifact; the point is that the guard let it by.
        assert (put.status_code, post.status_code) == (404, 404), (put.text, post.text)

    def test_ordinary_prose_is_accepted(self, answers_store) -> None:
        resp = client.put(
            self.URL.format("answers"),
            json={"answers": {"q1": "Users reset their password by email; tokens expire."}},
        )
        assert resp.status_code == 404, resp.text


class TestTheSecretIsWrittenNowhere:
    @pytest.mark.parametrize("body", BODIES, ids=CASE_IDS)
    def test_no_file_and_no_log_line_holds_the_secret(
        self, body, real_store, caplog, capfd
    ) -> None:
        caplog.set_level(logging.DEBUG)

        client.post("/api/v1/assessments", json=body)

        out, err = capfd.readouterr()
        needles = _encoded_forms(SECRET)
        assert _files_containing(real_store, needles) == []
        assert not any(n in caplog.text for n in needles)
        assert not any(n in out or n in err for n in needles)

    def test_nothing_starts_so_no_execution_input_exists(self, monkeypatch) -> None:
        calls: list[dict] = []

        class Recorder:
            def start_execution(self, job_id: str, sfn_input: dict) -> dict:
                calls.append(sfn_input)
                return {"execution_arn": "arn:test", "start_date": "now"}

        monkeypatch.setattr(assessments, "sfn_service", Recorder())
        monkeypatch.setattr(assessments, "s3_service", object())
        for body in BODIES:
            client.post("/api/v1/assessments", json=body)
        assert calls == []


class TestTheForwardedFieldsAreAllowlisted:
    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "db.example.internal",
            "my-cluster.cluster-abc123.us-east-1.rds.amazonaws.com",
            "10.0.0.12",
            "::1",
            "[2001:db8::1]:5432",
            "db.example.internal:3306",
        ],
    )
    def test_a_host_name_or_ip_is_accepted(self, host, real_store) -> None:
        resp = client.post("/api/v1/assessments", json=_base(host=host, secret_arn=SECRET_ARN))
        assert resp.status_code == 202, resp.text
        assert _meta(real_store, resp.json()["job_id"])["input"]["connection"]["host"] == host

    @pytest.mark.parametrize(
        "host",
        [
            "db/app",
            "db=1",
            "db app",
            "db;x",
            "app@db",
            "jdbc:oracle:thin:@db:1521:orcl",
            "mysql://app@db/app",
            "db:99999",
            "-db",
            "a..b",
            "",
        ],
    )
    def test_anything_else_in_host_is_a_422(self, host, real_store) -> None:
        resp = client.post("/api/v1/assessments", json=_base(host=host, secret_arn=SECRET_ARN))
        assert resp.status_code == 422, resp.text
        assert not (real_store / "test_db").exists()

    @pytest.mark.parametrize("database", ["app", "orcl.example.com", "my_db$1", "app-prod"])
    def test_a_plain_database_name_is_accepted(self, database, real_store) -> None:
        body = _base(database=database, secret_arn=SECRET_ARN)
        assert client.post("/api/v1/assessments", json=body).status_code == 202

    @pytest.mark.parametrize("database", ["app/x", "app=1", "a b", "u:p", "u@db", ""])
    def test_anything_else_in_database_is_a_422(self, database, real_store) -> None:
        body = _base(database=database, secret_arn=SECRET_ARN)
        assert client.post("/api/v1/assessments", json=body).status_code == 422

    @pytest.mark.parametrize("kind", ["Secrets-ARN", " secrets-arn "])
    def test_credentials_type_is_normalized(self, kind, real_store) -> None:
        resp = client.post(
            "/api/v1/assessments", json=_base(credentials_type=kind, secret_arn=SECRET_ARN)
        )
        assert resp.status_code == 202, resp.text
        connection = _meta(real_store, resp.json()["job_id"])["input"]["connection"]
        assert connection["credentials_type"] == "secrets-arn"

    @pytest.mark.parametrize("kind", ["iam", "plain", ""])
    def test_any_other_credentials_type_is_a_422(self, kind, real_store) -> None:
        resp = client.post(
            "/api/v1/assessments", json=_base(credentials_type=kind, secret_arn=SECRET_ARN)
        )
        assert resp.status_code == 422, resp.text


class TestASecretReferenceIsAccepted:
    @pytest.mark.parametrize(
        "connection",
        [
            {"credentials_type": "secrets-arn", "secret_arn": SECRET_ARN},
            {"credentials_type": "secrets-name", "secret_name": SECRET_NAME},
        ],
        ids=["secret-arn", "secret-name"],
    )
    def test_a_reference_without_a_password_starts_the_job(self, connection, real_store) -> None:
        resp = client.post("/api/v1/assessments", json=_base(**connection))

        assert resp.status_code == 202, resp.text
        meta = _meta(real_store, resp.json()["job_id"])
        sent = meta["input"]["connection"]
        for key, value in connection.items():
            assert sent[key] == value
        if "secret_arn" in connection:
            assert meta["input"]["secret_arn"] == SECRET_ARN
        assert not [k for k in sent if re.search(r"pass|pwd|user", k, re.IGNORECASE)]

    def test_secret_words_in_table_patterns_are_not_credentials(self, real_store) -> None:
        body = _with_options(
            include_table_patterns="password_resets,api_tokens,user_secrets%",
            exclude_table_patterns="oauth_access_tokens",
        )
        assert client.post("/api/v1/assessments", json=body).status_code == 202

    @pytest.mark.parametrize("empty", [None, ""], ids=["null", "empty-string"])
    def test_an_empty_password_carries_no_secret_and_is_not_forwarded(
        self, empty, real_store
    ) -> None:
        resp = client.post("/api/v1/assessments", json=_base(secret_arn=SECRET_ARN, password=empty))

        assert resp.status_code == 202, resp.text
        meta = (real_store / "test_db" / resp.json()["job_id"] / "_meta.json").read_text()
        assert "password" not in meta

    def test_offline_mode_still_needs_no_connection(self, real_store) -> None:
        body = {
            "source_database_type": "mysql",
            "database_name": "test_db",
            "collection_mode": "offline",
            "offline_s3_key": "test_db/job/uploads/collector-output.json",
        }
        assert client.post("/api/v1/assessments", json=body).status_code == 202


class TestTheExecutionInputCarriesNoPassword:
    def test_the_connection_model_has_no_password_or_username_field(self) -> None:
        names = set(ConnectionDetails.model_json_schema()["properties"])
        assert not [n for n in names if re.search(r"pass|pwd|user", n, re.IGNORECASE)], names

    def test_an_old_meta_file_with_a_password_still_loads_without_it(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        store.write_json("test_db/job-1/collector/output.json", {"metadata": {}})
        store.write_json("test_db/job-1/_meta.json", _old_meta())

        execution = LocalExecutionService(store).describe_execution("job-1")

        assert execution is not None
        assert execution["input"]["database_name"] == "test_db"
        assert execution["input"]["connection"]["host"] == "db"
        assert SECRET not in json.dumps(execution)

    def test_start_execution_writes_no_meta_for_an_input_with_a_secret(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        sfn_input = {"database_name": "test_db", "connection": {"host": "db", "password": SECRET}}

        with pytest.raises(ValueError, match="secret"):
            LocalExecutionService(store).start_execution("job-1", sfn_input)

        assert _files_containing(tmp_path, [SECRET]) == []
        assert not (tmp_path / "test_db" / "job-1" / "_meta.json").exists()


def _old_meta() -> dict:
    """A _meta.json as API versions before #723 wrote it."""
    return {
        "job_id": "job-1",
        "database_name": "test_db",
        "started_at": "2026-10-01T00:00:00+00:00",
        "input": {
            "database_name": "test_db",
            "source_database_type": "mysql",
            "secret_arn": "",
            "connection": {
                "host": "db",
                "credentials_type": "direct",
                "username": "app",
                "password": SECRET,
            },
        },
    }


class TestStoredSecretsAreScrubbed:
    def _seed(self, root: Path) -> LocalArtifactStore:
        store = LocalArtifactStore(base_dir=str(root))
        store.write_json("test_db/job-1/_meta.json", _old_meta())
        store.write_json(
            "test_db/job-1/collector/answers.json",
            {"answers": {"q1": URI, "q2": "nightly batch"}, "agent_type": "collector"},
        )
        clean = {"job_id": "job-2", "input": {"connection": {"secret_arn": SECRET_ARN}}}
        store.write_json("test_db/job-2/_meta.json", clean)
        return store

    def test_an_old_file_is_rewritten_without_the_secret(self, tmp_path) -> None:
        store = self._seed(tmp_path)

        run = redact_stored_inputs(tmp_path)

        assert run.rewritten == [
            "test_db/job-1/_meta.json",
            "test_db/job-1/collector/answers.json",
        ]
        assert run.skipped == []
        assert _files_containing(tmp_path, _encoded_forms(SECRET)) == []
        meta = store.read_json("test_db/job-1/_meta.json")
        assert meta["input"]["connection"] == {
            "host": "db",
            "credentials_type": "direct",
            "username": "app",
        }
        assert meta["input"]["database_name"] == "test_db"
        answers = store.read_json("test_db/job-1/collector/answers.json")
        assert answers["answers"] == {"q1": "<redacted>", "q2": "nightly batch"}

    @pytest.mark.parametrize(
        "answer",
        [
            "Owner:dba@example.com",
            "pin to @scope/pkg@1.2.3",
            "auth token: none needed",
            "meet at 10:30@UTC",
            "the password policy is 90 days",
            "session token: rotates daily",
            "users.password: bcrypt hash",
            "password: hashed",
            "x" * (MAX_SCANNED_CHARS + 10),
        ],
        ids=[
            "email-after-label",
            "npm-scope",
            "token-none",
            "time",
            "prose",
            "token-label",
            "column-description",
            "password-label",
            "over-long",
        ],
    )
    def test_text_that_holds_no_credential_is_kept(self, answer, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        store.write_json("db/job/collector/answers.json", {"answers": {"q1": answer}})

        assert redact_stored_inputs(tmp_path).rewritten == []
        assert store.read_json("db/job/collector/answers.json")["answers"]["q1"] == answer

    @pytest.mark.parametrize(
        "answer",
        [
            URI,
            "Server=db;Password=" + SECRET,
            "Password = " + SECRET,
            "mysql --password " + SECRET,
            "ALTER USER app PASSWORD '" + SECRET + "'",
        ],
        ids=["uri", "ado", "spaced-assignment", "mysql-flag", "alter-user"],
    )
    def test_an_old_answer_with_a_credential_is_redacted(self, answer, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        store.write_json("db/job/collector/answers.json", {"answers": {"q1": answer}})

        assert redact_stored_inputs(tmp_path).rewritten == ["db/job/collector/answers.json"]
        assert store.read_json("db/job/collector/answers.json")["answers"]["q1"] == "<redacted>"

    def test_long_strings_and_deep_values_in_meta_are_kept(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        deep: dict = {"leaf": "kept"}
        for _ in range(40):
            deep = {"n": deep}
        meta = _old_meta()
        meta["input"]["error"] = "Traceback ... " + "y" * (MAX_SCANNED_CHARS + 10)
        meta["input"]["deep"] = deep
        store.write_json("db/job/_meta.json", meta)

        assert redact_stored_inputs(tmp_path).rewritten == ["db/job/_meta.json"]
        cleaned = store.read_json("db/job/_meta.json")
        assert cleaned["input"]["error"] == meta["input"]["error"]
        assert cleaned["input"]["deep"] == deep
        assert SECRET not in json.dumps(cleaned)

    def test_it_is_idempotent_and_leaves_clean_files_alone(self, tmp_path) -> None:
        self._seed(tmp_path)
        clean = tmp_path / "test_db" / "job-2" / "_meta.json"
        inode = clean.stat().st_ino

        redact_stored_inputs(tmp_path)
        first = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
        second_run = redact_stored_inputs(tmp_path)

        assert second_run.rewritten == [] and second_run.skipped == []
        assert {p: p.read_bytes() for p in tmp_path.rglob("*.json")} == first
        assert clean.stat().st_ino == inode

    def test_symlinks_and_dot_folders_are_not_followed(self, tmp_path) -> None:
        outside = tmp_path / "outside"
        LocalArtifactStore(base_dir=str(outside)).write_json("x/y/_meta.json", _old_meta())
        root = tmp_path / "artifacts"
        (root / "db").mkdir(parents=True)
        (root / "db" / "job").symlink_to(outside / "x" / "y")
        LocalArtifactStore(base_dir=str(root)).write_json(".trash/j/_meta.json", _old_meta())

        run = redact_stored_inputs(root)

        assert run.rewritten == []
        assert run.skipped == ["db/job/_meta.json"]  # refused by the store's containment
        assert SECRET in (outside / "x" / "y" / "_meta.json").read_text()

    def test_a_read_only_file_is_skipped_and_the_rest_still_redacted(
        self, tmp_path, monkeypatch
    ) -> None:
        self._seed(tmp_path)
        LocalArtifactStore(base_dir=str(tmp_path)).write_json("ro_db/job/_meta.json", _old_meta())
        # Simulate a file the process can't rewrite. chmod doesn't do it for every user (root
        # writes read-only folders), so the write itself fails the way the OS reports it.
        real_write = LocalArtifactStore.write_json

        def write_json(store, key, data, *args, **kwargs):
            if key.startswith("ro_db/"):
                raise PermissionError(13, "Permission denied", key)
            return real_write(store, key, data, *args, **kwargs)

        monkeypatch.setattr(LocalArtifactStore, "write_json", write_json)
        run = redact_stored_inputs(tmp_path)

        assert run.skipped == ["ro_db/job/_meta.json"]
        assert "test_db/job-1/_meta.json" in run.rewritten
        assert SECRET not in (tmp_path / "test_db" / "job-1" / "_meta.json").read_text()

    def test_a_malformed_or_deeply_nested_file_is_skipped(self, tmp_path) -> None:
        self._seed(tmp_path)
        bad = tmp_path / "bad_db" / "job"
        bad.mkdir(parents=True)
        (bad / "_meta.json").write_text("[" * 100_000 + "]" * 100_000)
        nested = tmp_path / "bad_db" / "job2"
        nested.mkdir()
        (nested / "_meta.json").write_text("{not json")

        run = redact_stored_inputs(tmp_path)

        assert sorted(run.skipped) == ["bad_db/job/_meta.json", "bad_db/job2/_meta.json"]
        assert "test_db/job-1/_meta.json" in run.rewritten

    def test_the_api_redacts_once_in_the_background_after_start_up(
        self, tmp_path, monkeypatch
    ) -> None:
        self._seed(tmp_path)
        monkeypatch.setattr(api_main, "_artifact_store", LocalArtifactStore(str(tmp_path)))

        with TestClient(app, base_url="http://localhost") as started:
            assert started.get("/health").status_code == 200
            api_main.redaction_thread.join(timeout=30)

        assert _files_containing(tmp_path, [SECRET]) == []
        assert (tmp_path / MARKER).exists()

    def test_a_recorded_run_is_not_repeated_at_start_up(self, tmp_path, monkeypatch) -> None:
        LocalArtifactStore(str(tmp_path)).write_json(MARKER, {"completed_at": "earlier"})
        calls: list[object] = []
        monkeypatch.setattr(
            stored_input_redaction, "redact_stored_inputs", lambda root: calls.append(root)
        )
        monkeypatch.setattr(api_main, "_artifact_store", LocalArtifactStore(str(tmp_path)))

        with TestClient(app):
            api_main.redaction_thread.join(timeout=30)

        assert calls == []

    def test_a_skipped_file_leaves_no_marker_so_the_next_start_retries(self, tmp_path) -> None:
        bad = tmp_path / "db" / "job"
        bad.mkdir(parents=True)
        (bad / "_meta.json").write_text("{not json")

        run = redact_stored_inputs_once(tmp_path)

        assert run is not None and run.skipped == ["db/job/_meta.json"]
        assert not (tmp_path / MARKER).exists()

    def test_a_failing_run_never_stops_the_api(self, tmp_path, monkeypatch) -> None:
        def explode(root):
            raise RecursionError("boom")

        escaped: list[object] = []
        monkeypatch.setattr(threading, "excepthook", lambda args: escaped.append(args))
        monkeypatch.setattr(stored_input_redaction, "redact_stored_inputs", explode)
        monkeypatch.setattr(api_main, "_artifact_store", LocalArtifactStore(str(tmp_path)))

        with TestClient(app, base_url="http://localhost") as started:
            api_main.redaction_thread.join(timeout=30)
            assert started.get("/health").status_code == 200
        assert escaped == []  # handled inside the run, not left to kill the thread

    def test_the_api_answers_while_the_run_is_still_going(self, tmp_path, monkeypatch) -> None:
        release = threading.Event()
        order: list[str] = []

        def slow(root):
            release.wait(timeout=10)
            order.append("redaction finished")
            return stored_input_redaction.RedactionRun()

        monkeypatch.setattr(stored_input_redaction, "redact_stored_inputs", slow)
        monkeypatch.setattr(api_main, "_artifact_store", LocalArtifactStore(str(tmp_path)))

        with TestClient(app, base_url="http://localhost") as started:
            assert started.get("/health").status_code == 200
            order.append("health answered")
            release.set()
            api_main.redaction_thread.join(timeout=30)

        assert order == ["health answered", "redaction finished"]

    def test_the_script_redacts_and_prints_paths_only(self, tmp_path, capsys) -> None:
        from scripts.redact_stored_inputs import main

        self._seed(tmp_path)
        assert main(["--artifact-dir", str(tmp_path)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["rewritten_count"] == 2
        assert out["skipped_files"] == []
        assert SECRET not in json.dumps(out)
        assert _files_containing(tmp_path, [SECRET]) == []
        assert (tmp_path / MARKER).exists()


class TestEveryCheckIsBounded:
    def test_a_body_over_the_api_limit_is_a_413(self, real_store) -> None:
        big = json.dumps({**_base(secret_arn=SECRET_ARN), "pad": "x" * (2 * 1024 * 1024)})
        resp = client.post(
            "/api/v1/assessments", content=big, headers={"content-type": "application/json"}
        )
        assert resp.status_code == 413
        assert not (real_store / "test_db").exists()

    def test_a_chunked_body_without_a_length_is_capped_too(self, real_store) -> None:
        def chunks():
            for _ in range(40):
                yield b"x" * 65536

        resp = client.post(
            "/api/v1/assessments", content=chunks(), headers={"content-type": "application/json"}
        )
        assert resp.status_code == 413

    def test_a_guarded_body_over_its_own_limit_is_a_413(self, real_store) -> None:
        # Many short fields: each is under the per-field cap, the body is over its own.
        body = {**_base(secret_arn=SECRET_ARN), "pad": ["t"] * (MAX_GUARDED_BODY_BYTES // 4)}
        assert len(json.dumps(body)) > MAX_GUARDED_BODY_BYTES
        assert client.post("/api/v1/assessments", json=body).status_code == 413

    def test_a_field_too_long_to_check_is_a_413(self, real_store) -> None:
        body = _with_options(include_table_patterns="t" * (MAX_SCANNED_CHARS + 1))
        resp = client.post("/api/v1/assessments", json=body)
        assert resp.status_code == 413, resp.text
        assert "options.include_table_patterns" in resp.json()["detail"]

    def test_deep_nesting_is_refused_not_a_500(self, real_store) -> None:
        for depth in (100, 3000):
            raw = '{"a": ' * depth + "1" + "}" * depth
            resp = client.post(
                "/api/v1/assessments", content=raw, headers={"content-type": "application/json"}
            )
            assert resp.status_code == 413, (depth, resp.status_code)

    def test_a_1mb_value_is_refused_at_a_small_multiple_of_an_ordinary_request(
        self, real_store
    ) -> None:
        big = json.dumps(_base(secret_arn=SECRET_ARN, host="a" * (1024 * 1024)))
        small = json.dumps(_with_options(include_table_patterns="orders,users"))
        headers = {"content-type": "application/json"}
        assert client.post("/api/v1/assessments", content=big, headers=headers).status_code == 413

        # Relative to an ordinary request in this same process (#938): before the caps and
        # the rewrite, a 200 KB host alone took over a minute.
        assert_cost_ratio(
            lambda: client.post("/api/v1/assessments", content=big, headers=headers),
            lambda: client.post("/api/v1/assessments", content=small, headers=headers),
            max_ratio=20.0,
        )

    @pytest.mark.parametrize(
        "unit",
        ["a", "QUFB", "a:", "@", "a@", "a=", ":/", "%41", "mysql -pa ", '{"a":'],
        ids=[
            "letters",
            "base64",
            "colons",
            "ats",
            "userinfo",
            "assignments",
            "scheme",
            "percent",
            "mysql",
            "json",
        ],
    )
    def test_the_string_check_is_linear_without_the_cap(self, unit) -> None:
        # The caps bound the input; this shows the parsing itself has no super-linear path.
        assert_linear(
            lambda size: (unit * (size // len(unit) + 1))[:size],
            lambda text: secret_guard._string_holds_secret(text, 0),
        )

    def test_the_scan_runs_off_the_event_loop(self, real_store, monkeypatch) -> None:
        calls: list[object] = []
        original = secret_guard.run_in_threadpool

        async def recording(fn, *args):
            calls.append(fn)
            return await original(fn, *args)

        monkeypatch.setattr(secret_guard, "run_in_threadpool", recording)
        client.post("/api/v1/assessments", json=_base(secret_arn=SECRET_ARN))
        assert calls == [secret_guard.inspect_request]


class TestTheServerLogsNoSecret:
    # uvicorn's access logger (h11_impl / httptools_impl) logs
    # '%s - "%s %s HTTP/%s" %d' with args (client_addr, method, full_path, http_version,
    # status_code); these records follow that layout.
    def test_the_access_log_drops_a_query_that_carries_a_secret(self) -> None:
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5000", "POST", f"/api/v1/assessments?password={SECRET}", "1.1", 400),
            None,
        )
        assert RedactSecretQueryFilter().filter(record)
        assert SECRET not in record.getMessage()
        assert "/api/v1/assessments?<redacted>" in record.getMessage()

    def test_an_ordinary_query_is_logged_unchanged(self) -> None:
        path = "/api/v1/assessments?status=RUNNING&limit=25"
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5000", "GET", path, "1.1", 200),
            None,
        )
        RedactSecretQueryFilter().filter(record)
        assert path in record.getMessage()

    def test_the_filter_is_installed_on_the_access_logger(self) -> None:
        filters = logging.getLogger("uvicorn.access").filters
        assert any(isinstance(f, RedactSecretQueryFilter) for f in filters)


class TestTheDetector:
    @pytest.mark.parametrize("body", BODIES, ids=CASE_IDS)
    def test_every_form_is_found(self, body) -> None:
        assert find_submitted_secrets(body)

    def test_it_reports_paths_not_values(self) -> None:
        found = find_submitted_secrets(_base(password=SECRET, replicas=[{"token": SECRET}]))
        assert found == ["connection.password", "connection.replicas[0].token"]

    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "db_password",
            "dbPassword",
            "PGPASSWORD",
            "MYSQL_PWD",
            "client_secret",
            "auth_token",
            "api_key",
            "apiKey",
            "aws_secret_access_key",
            "private_key",
            "credentials",
            "pwd",
            "pass",
            "PassWord",
            "dbPassWord",
            "pass_word",
            "password_hash",
            "secret_value",
            "SecretString",
        ],
    )
    def test_secret_keys(self, key) -> None:
        assert is_secret_key(key)

    @pytest.mark.parametrize(
        "key",
        [
            "max_tokens",
            "token_budget",
            "secret_rotation_days",
            "use_secrets_manager",
            "credentials_note",
            "api_key_header",
            "secret_arn",
            "secret_name",
            "secretArn",
            "credentials_type",
            "username",
            "host",
            "passthrough",
            "bypass",
        ],
    )
    def test_keys_that_only_mention_a_secret(self, key) -> None:
        assert not is_secret_key(key)

    @pytest.mark.parametrize(
        "body",
        [
            {},
            [],
            _base(secret_arn=SECRET_ARN, credentials_type="secrets-arn"),
            _base(secret_name=SECRET_NAME),
            _with_options(sample_size=1000, query_log_source="performance-insights"),
            {"database_name": "testdb12"},  # valid base64, decodes to nothing readable
            {"note": "rotate the password monthly"},  # prose, no assignment
            {"note": "contact dba@example.com"},  # an e-mail address is not user info
            {"host": "jdbc:oracle:thin:@db:1521:orcl"},  # JDBC thin without credentials
            {"llm": {"max_tokens": 4096}},
            {"q": "WHERE token = ?"},  # spaced SQL comparison, not an assignment
            {"q": "UPDATE users SET password = :new_password"},  # a bind parameter
            {"note": "see mysql://app@db.example.internal/app"},  # user, no password
            {"q": "auth token: none needed"},
        ],
        ids=[
            "empty-dict",
            "empty-list",
            "arn",
            "name",
            "options",
            "b64-lookalike",
            "prose",
            "email",
            "jdbc-thin-no-creds",
            "max-tokens",
            "sql-comparison",
            "bind-parameter",
            "uri-no-password",
            "token-none",
        ],
    )
    def test_ordinary_requests_are_clean(self, body) -> None:
        assert find_submitted_secrets(body) == []


class TestTheUiSendsNoPassword:
    """A tripwire from the Python suite: the Create Analysis page never grows a password
    field back. The behaviour (the rendered form and the request it sends) is tested in
    Jest: ``src/ui/src/__tests__/CreateAnalysis.credentials.test.js`` and
    ``src/ui/src/utils/__tests__/connectionPayload.test.js``."""

    UI = Path(__file__).resolve().parents[3] / "src" / "ui" / "src"

    def test_the_page_has_no_password_or_direct_credentials_field(self) -> None:
        page = (self.UI / "pages" / "CreateAnalysis.js").read_text()
        assert not re.search(r"type=[\"']password[\"']", page)
        assert not re.search(r"formData\.(password|username)\b", page)
        assert not re.search(r"[\"']direct[\"']", page)
        assert "buildConnection(formData)" in page

    def test_the_connection_builder_sends_only_reference_fields(self) -> None:
        builder = (self.UI / "utils" / "connectionPayload.js").read_text()
        sent = set(re.findall(r"connection\.(\w+)\s*=", builder))
        sent |= set(re.findall(r"^\s{4}(\w+):", builder, re.MULTILINE))
        assert sent == {"host", "port", "database", "credentials_type", "secret_arn", "secret_name"}


# Credential forms that round 2 still let through the other free-text fields.
FREE_TEXT_FORMS = {
    "spaced-assignment": "Password = " + SECRET,
    "mixed-case-key": "PassWord=" + SECRET,
    "split-key": "pass_word=" + SECRET,
    "spaced-colon": "password : " + SECRET,
    "mysql-long-flag": "mysql --password " + SECRET,
    "sqlcmd-P": "sqlcmd -U sa -P " + SECRET,
    "sqlcmd-P-attached": "sqlcmd -U sa -P" + SECRET,
    "sqlplus-user-slash-password": "sqlplus scott/" + SECRET,  # pragma: allowlist secret
    "identified-by": "ALTER USER app IDENTIFIED BY " + SECRET,
    "create-role-password": "CREATE ROLE app WITH LOGIN PASSWORD '" + SECRET + "'",
    "alter-role-password": "ALTER ROLE app PASSWORD '" + SECRET + "'",
    "mongosh-p": "mongosh -u app -p " + SECRET,
    "redis-cli-a": "redis-cli -h cache -a " + SECRET,
    "redis-cli-a-attached": "redis-cli -a" + SECRET,
    "dollar-not-a-bind-parameter": "Password = $1" + SECRET,
}

FREE_TEXT_FIELDS = {
    "secret_name": lambda v: _base(secret_name=v),
    "secret_arn": lambda v: _base(secret_arn=v),
    "include_table_patterns": lambda v: _with_options(include_table_patterns=v),
    "exclude_table_patterns": lambda v: _with_options(exclude_table_patterns=v),
    "query_log_source": lambda v: _with_options(query_log_source=v),
    "target_databases": lambda v: {**_base(secret_arn=SECRET_ARN), "target_databases": [v]},
    "source_database_type": lambda v: {**_base(secret_arn=SECRET_ARN), "source_database_type": v},
}


class TestTheOtherFreeTextFields:
    @pytest.mark.parametrize("form", FREE_TEXT_FORMS.values(), ids=FREE_TEXT_FORMS.keys())
    def test_each_form_is_a_credential(self, form) -> None:
        assert find_submitted_secrets({"note": form})

    @pytest.mark.parametrize("field_name", FREE_TEXT_FIELDS.keys())
    @pytest.mark.parametrize("form", FREE_TEXT_FORMS.values(), ids=FREE_TEXT_FORMS.keys())
    def test_refused_with_a_400_in_every_field(self, form, field_name, real_store) -> None:
        resp = client.post("/api/v1/assessments", json=FREE_TEXT_FIELDS[field_name](form))
        assert resp.status_code == 400, resp.text
        assert _files_containing(real_store, [SECRET]) == []

    @pytest.mark.parametrize(
        ("form", "field_name"),
        [
            pytest.param(form, name, id=f"{form_id}-{name}")
            for form_id, form in FREE_TEXT_FORMS.items()
            for name in FREE_TEXT_FIELDS
            # AWS allows '=' in a secret name, so a spaceless "key=value" is a valid name:
            # there the detector (test_refused_with_a_400_in_every_field) is the only layer.
            if not (name == "secret_name" and " " not in form)
        ],
    )
    def test_the_field_allowlist_alone_also_refuses_it(
        self, form, field_name, real_store, monkeypatch
    ) -> None:
        # With the detector switched off, the model's allowlist is the second layer.
        monkeypatch.setattr(
            secret_guard, "inspect_request", lambda *a, **k: secret_guard.Findings()
        )
        resp = client.post("/api/v1/assessments", json=FREE_TEXT_FIELDS[field_name](form))
        assert resp.status_code == 422, resp.text
        assert _files_containing(real_store, [SECRET]) == []

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("secret_name", "prod/db/app-creds_v2"),
            ("secret_name", "rds!db-3f1c2a9e-8b7d-4e6f-9a0b-1c2d3e4f5a6b"),
            ("secret_name", "rds!cluster-0c4b8f0e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"),
            (
                "secret_arn",
                "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
                "rds!db-3f1c2a9e-8b7d-4e6f-9a0b-1c2d3e4f5a6b-AbCdEf",
            ),
            (
                "secret_arn",
                "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
                "rds!cluster-0c4b8f0e-1a2b-4c3d-8e9f-0a1b2c3d4e5f-AbCdEf",
            ),
            ("include_table_patterns", '"Order Details", orders'),
            ("include_table_patterns", "[dbo].[Order Details],audit_%"),
            ("exclude_table_patterns", "`Order Details`"),
            ("include_table_patterns", "orders, order_items, audit_%"),
            ("exclude_table_patterns", "tmp_*,$staging"),
            ("query_log_source", "pg-stat-statements"),
            ("target_databases", "dynamodb"),
            ("source_database_type", "sqlserver"),
        ],
    )
    def test_ordinary_values_are_accepted(self, field_name, value, real_store) -> None:
        resp = client.post("/api/v1/assessments", json=FREE_TEXT_FIELDS[field_name](value))
        assert resp.status_code == 202, resp.text

    def test_an_over_long_token_before_an_equals_sign_is_not_a_key(self) -> None:
        padded = "A" * 300 + "=="
        assert find_submitted_secrets({"note": padded}) == []


def _nested_base64_json(levels: int) -> str:
    """``levels`` of base64-wrapped JSON lists around plain text (no credential)."""
    text = "nightly batch window"
    for _ in range(levels):
        text = _b64(json.dumps([text]))
    return text


class TestNestedEncodingIsBounded:
    def test_nested_base64_json_costs_a_small_multiple_of_flat_text(self) -> None:
        deep = _nested_base64_json(16)
        assert len(deep) <= MAX_SCANNED_CHARS
        flat = ("nightly batch " * (len(deep) // 14 + 1))[: len(deep)]

        # Before the de-duplication each level doubled the work (10 levels: 7.8 ms, against
        # 0.11 ms now; the review measured 567 ms at 16 levels).
        assert_cost_ratio(
            lambda: find_submitted_secrets({"a": deep}),
            lambda: find_submitted_secrets({"a": flat}),
            max_ratio=25.0,
        )

    @pytest.mark.parametrize("levels", [10, 12, 14, 17])
    def test_deeper_nesting_never_costs_more_than_three_levels(self, levels) -> None:
        deep = _nested_base64_json(levels)
        assert len(deep) <= MAX_SCANNED_CHARS
        three = _nested_base64_json(3)
        assert find_submitted_secrets({"a": deep}) == []
        assert_cost_ratio(
            lambda: find_submitted_secrets({"a": deep}),
            lambda: find_submitted_secrets({"a": three + " " * (len(deep) - len(three))}),
            max_ratio=25.0,
        )

    def test_a_secret_three_levels_down_is_still_found(self) -> None:
        text = "Password=" + SECRET
        for _ in range(2):
            text = _b64(json.dumps({"a": text}))
        assert find_submitted_secrets({"a": text})

    def test_more_decoded_text_than_the_budget_is_too_large_to_check(self) -> None:
        chunk = _b64("x " * 2500)  # 5000 decoded characters each, 6668 encoded
        small = secret_guard.scan({"a": [chunk] * 10})
        large = secret_guard.scan({"a": [chunk] * 60})
        assert not small
        assert large.too_large and not large.secrets

    @pytest.mark.parametrize(
        "text",
        [_b64("plain words here"), quote(_b64("plain words"), safe=""), _nested_base64_json(4)],
        ids=["base64", "percent-of-base64", "nested"],
    )
    def test_each_decoded_variant_is_checked_once(self, text) -> None:
        variants = secret_guard._decoded_variants(text, secret_guard._Budget())
        assert len(variants) == len(set(variants)) > 1


class TestQuotedTableNames:
    @pytest.mark.parametrize(
        "pattern",
        [
            "Order Details",  # a space outside quotes
            '"Order Details',  # unclosed
            '"mysql --password x"',  # a client flag inside quotes
            "[a=b]",
            '"app:x@db"',
            "\"pass 'x'\"",
            '"" ',
        ],
    )
    def test_anything_but_plain_words_inside_quotes_is_refused_by_the_allowlist(
        self, pattern
    ) -> None:
        # The model alone, without the detector in front of it.
        with pytest.raises(ValidationError, match="quote a name with spaces"):
            CollectionOptions(include_table_patterns=pattern)

    def test_the_bind_parameter_rule_only_spares_dollar_digits(self) -> None:
        assert find_submitted_secrets({"q": "UPDATE t SET password = $1"}) == []
        assert find_submitted_secrets({"q": "Password = $1xZq7abc"})
        assert find_submitted_secrets({"q": "password = @p1"}) == []  # @name binds
