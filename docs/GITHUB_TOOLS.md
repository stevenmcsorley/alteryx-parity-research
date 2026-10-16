# Flowbench GitHub tools

Both tools use a saved **GitHub** connection (toolbar → Web & API). The token stays in the OS
credential vault and is sent only under the connection's API base URL. Set the base URL to
`https://github.example.com/api/v3` for GitHub Enterprise. Both need the run's network permission.
See [connections](CONNECTIONS.md).

## GitHub Get Last Commit (`core.github_last_commit`, `developer:github-get-last-commit`)

There is no input. The tool returns one record for the latest commit on `branch` (the repository's
default branch when blank). With `path` set, it returns the latest commit that changed that file or
folder. Fields are `Repository`, `Branch`, `Path`, `SHA`, `Author`, `AuthorEmail`, `CommitDate` (UTC),
`Message` and `URL`. No matching commit is an error.

## GitHub Commit (`core.github_commit`, `developer:github-commit`)

Every input record becomes a file: `path_field` holds its repository path and `content_field` its
content (text is UTF-8 encoded, and blobs are sent byte for byte). All records go into **one commit** on an
existing `branch` with `message`, keeping every other file in the branch unchanged.

- It pushes outside this computer, so it needs the **remote-write** permission in addition to network
  access (CLI `--allow-remote-writes`). The desktop asks separately, and the preflight lists it under
  `remote_writes`.
- The branch update is never forced. If someone else pushes while the commit is being built, the run
  fails with GitHub's "not a fast forward" error and their commit is kept.
- Paths must be relative, use `/` and contain no empty, `.` or `..` segments. Repeated paths and null
  content are errors. No input records means no commit.
- Output is the input records plus `BlobSHA`, `CommitSHA` and `CommitURL`.

These are Flowbench semantics derived from the public tool descriptions. Deletions, file modes other
than `100644`, branch creation, pull requests, author overrides, the vendor macros' other modes and
Alteryx XML import are not implemented or claimed. Tests: `tests/test_github_tools.py`, using a local
fake GitHub API with no external network.
