# FastAPI REST API

This directory contains the FastAPI REST API server.

## Files

- **main.py** - FastAPI application with endpoints

## Endpoints

- `GET /health` - Health check
- `POST /api/v1/analyses` - Create analysis job
- `GET /api/v1/analyses/{job_id}` - Get analysis status
- `GET /api/v1/analyses/{job_id}/results` - Get analysis results
- `WS /ws/analyses/{job_id}` - WebSocket for real-time updates

## Running

```bash
uvicorn src.api.main:app --reload --port 8000
```

## Credentials are never sent (ADR-004 Decision 5)

A request names the database credentials by reference only: `connection.secret_arn` (a Secrets Manager ARN) or `connection.secret_name` (a secret name: letters, digits and `/_+=.@!-`, so RDS-managed `rds!db-...` and `rds!cluster-...` secrets work). The other forwarded fields are allowlisted too: `connection.host` is a host name or IP address (optionally `host:port`), `connection.database` a plain name, `connection.credentials_type` is `secrets-arn` or `secrets-name`, the engine, source and mode fields are plain names, and table patterns are table names and wildcards separated by commas (quote a name with spaces: `"Order Details"`, `[Order Details]` or `` `Order Details` ``). A request that carries a password, secret, token or a credential-shaped string anywhere is refused with a 400 and nothing is stored (`secret_guard.py`). Agent answers are free text, so there only a value with a credential shape is refused (an assignment of a literal, a client flag with a value, user info in a connection string); `users.password: bcrypt hash` or `token: rotates daily` is accepted. Request bodies over 1 MiB get a 413 (`MODERNIZER_MAX_BODY_BYTES` overrides the limit).

Versions before #723 could store a submitted password in `artifacts/<db>/<job>/_meta.json`. When the API starts, a background thread removes any such stored credential (and any credential-shaped agent answer) once, and records `artifacts/.meta/_stored-input-redaction-v1.json` so later starts skip it. A file it cannot read or write is logged by path and skipped, the API starts regardless, and the marker is only recorded when nothing was skipped. Only credentials are removed: long values, deeply nested values and text that merely resembles a credential are kept as they are. To run it by hand, whatever the marker says:

```bash
uv run python scripts/redact_stored_inputs.py --artifact-dir ./artifacts
```

It prints the rewritten and skipped file paths (never their contents) and is safe to run more than once.

Live collection reads the secret named by either form. The API forwards `connection.secret_arn` or `connection.secret_name` as the collector's one secret id (`secret_arn` in the execution input, `SECRET_ARN` for the collector), and the collector passes it to Secrets Manager as `SecretId`, which takes an ARN or a name. When both are given, `credentials_type` picks one (`secrets-arn` or `secrets-name`); when only one is given, that one is used. A name is looked up in the collector's own account and region; a secret shared from another account needs its full ARN.
