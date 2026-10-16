# Flowbench Download

`core.download` (catalogue entry `developer:download`) sends one HTTP(S) request per input record to
the URL in `url_field`. It appends `DownloadData` (text, or blob with `output: blob`), `DownloadHeaders`,
`StatusCode` and `DownloadError`. Output order always matches input order, including with
`connections` > 1.

- **Permission.** Requests can carry record data off the machine, so every run needs `allow_network`
  (CLI `--allow-network`). The desktop asks before the run. This includes runs requested by Flowbench
  AI, and a run never proceeds when the permission scan is incomplete. The preflight reports Download
  under `network_requests`.
- **Requests.** Methods are GET, POST, PUT, PATCH, DELETE and HEAD. Headers come from constant
  `headers` and from `header_fields` (named after the field). `query_fields` are URL-encoded onto the
  query string. Bodies come from one field (`body: field`), are form-encoded or JSON-encoded from
  `body_fields`, or are omitted. Null header, query and form values are omitted. Only absolute http and
  https URLs are accepted, and redirects to any other scheme are refused (`follow_redirects` can disable
  redirects entirely).
- **Responses.** A non-2xx status is data, not an error. Text decodes using the response charset, or
  `encoding` when none is declared. `max_response_bytes` (default 64 MiB) caps each response.
- **Failures.** Invalid URLs, network errors, timeouts (`timeout_seconds`), oversize responses and decode
  failures stop the run, naming the record (`on_error: error`). With `on_error: record`, they are written
  to `DownloadError` with a null status.
- URLs, header values and bodies are never written to run events. Only request counts are logged.
- **Authentication.** Set `connection_id` to a saved Web & API or GitHub connection. Its credential is added
  only to requests under the connection's base URL, and redirects that leave it are refused. See
  [connections](CONNECTIONS.md).

These are Flowbench semantics derived from the public Download tool description. DCM, NTLM
and Kerberos authentication, FTP, file-output modes, code-page selection and Alteryx XML import
are not implemented or claimed. Tests: `tests/test_download.py` (local HTTP server; no external network)
and `desktop/src/state/runPermissions.test.ts`.
