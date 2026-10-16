# Flowbench connections and credentials

Flowbench keeps credentials out of workflow files. A workflow stores only a connection id. The
non-secret settings are in a JSON file under `FLOWENGINE_HOME` (default `~/.flowbench`), and the
password or token is in the operating system credential vault: Windows Credential Manager, macOS
Keychain or Linux Secret Service. Headless CI can use the read-only `FLOWBENCH_VAULT=env` vault. Secrets
never appear in connection files, workflow documents, run events, manifests or history, and the engine
service never returns them.

| Toolbar | Kinds | Used by |
|---|---|---|
| **Databases** | PostgreSQL, SQL Server (`connections.json`) | Database Input/Output |
| **Web & API** | Web/API (bearer token, username and password, or API-key header) and GitHub (personal access token) (`service_connections.json`) | Download (`connection_id`), [GitHub Commit and Get Last Commit](GITHUB_TOOLS.md) |

## Scoping

A Web & API credential is attached only to requests under the connection's **base URL**: same scheme,
host and port, and a path at or below the base path. Paths containing `.` or `..` segments, including
percent-encoded ones, never match. Requests outside the base URL fail for that record and are sent
without the credential. Redirects that leave the base URL are refused, because HTTP clients forward
`Authorization` on redirect. Base URLs must use `https://`. Plain `http://` is accepted only for
localhost. A workflow header that would replace the connection's authentication header is a
configuration error.

GitHub connections default to `https://api.github.com`. Set the base URL to a GitHub Enterprise API
root (for example `https://github.example.com/api/v3`) to use one.

Engine service methods: `credentials.list` (optional `kind`), `credentials.save`
(`{connection, secret?}`; editing without `secret` keeps the saved one) and `credentials.delete`.
Every request still needs the run's network permission. Tests: `tests/test_service_connections.py`,
`desktop/src/panels/ServiceConnections.test.tsx`.
