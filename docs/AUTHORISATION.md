# Authorisation record for reference-software investigation

> Engineering record, not legal advice. Keep this file current before any further investigation.

## Basis

- **Date recorded:** 2026-10-13
- **Statement:** The repository owner stated in the working session that they hold **written authorisation** to investigate the locally installed reference software.
- **Authorisation reference / issuer / date:** _TO BE COMPLETED BY OWNER_
- **Scope as written in the authorisation:** _TO BE COMPLETED BY OWNER_ (e.g. black-box testing, configuration-file inspection, decompilation for interoperability, use for developing a competing product)
- **Licence/EULA terms considered:** Alteryx EULA §3.3 restricts reverse engineering and use to develop competing products; the authorisation must address these. UK CDPA s.50A–50C considered as background only.

Until the scope fields are completed, work is limited to the **conservative scope** below.

## Reference installation

- Product: Alteryx One + Designer, per-user install at `%LOCALAPPDATA%\Alteryx`
- Build observed: **2026.2.0.8** (`AlteryxGui.exe` / `AlteryxEngineCmd.exe` product version). This differs from the pinned public-catalogue target 2026.1.4.126; all evidence is labelled with the observed build.
- Binary digests (SHA-256, via REA): see `docs/evidence/rea-2026-10-13.json`.

## Conservative scope applied (2026-10-13)

Performed:

1. **Black-box differential execution**: running `AlteryxEngineCmd.exe` on workflow documents authored by this project, over original synthetic inputs, and reading the CSV outputs (`conformance/harness.py`). Generated documents and outputs stay in a temporary directory; only inputs we authored, configurations, observed output values for failing cases, and digests are committed.
2. **Structural scan** of workflow XML shipped with the install (sample workflows, bundled macros): tool identifiers, connection anchor names, configuration element *names* only (`conformance/reference/scan_reference_workflows.py` → `data/reference-observations.json`). Example configurations were read locally to learn the document format; no sample files, data or text were copied.
3. **REA static provenance**: PE resource inventory and SHA-256 of three binaries (no disassembly).

Not performed (out of conservative scope):

- Decompilation, disassembly or managed-metadata inspection of vendor assemblies/DLLs.
- Network interception or inspection of the Alteryx One cloud session.
- Reading `AlteryxOne\Tokens`, `EBWebView` profile data, settings or any credential store (explicitly excluded by every script).
- Copying vendor binaries, samples, icons, help text, macros or data into this repository.

## Rules for contributors

- Commit only original inputs, our configurations, observed facts (values/digests/names) and our analysis.
- Any change of scope requires updating this file first with the authorisation reference.
- `.gitignore` blocks vendor file types (`*.yxmd`, `*.yxmc`, `*.dll`, `*.exe`, …).
