# Authorisation record for reference-software investigation

> Engineering record, not legal advice. Keep this file current before any further investigation.

## Basis

- **Statements recorded (2026-10-13, from the repository owner):**
  1. The owner personally spoke with Alteryx by telephone about this project.
  2. Following that conversation, the owner reports having received **written authorisation** from Alteryx relating to the project.
- These are the owner's statements of fact; the project treats the authorisation as obtained.
- **Authorisation document reference / issuer / date:** _owner to record (e.g. email or letter reference, Alteryx contact, date)_
- **Activities the written authorisation covers:** _owner to record verbatim or summarise (e.g. black-box testing, configuration/format inspection, decompilation for interoperability, use for developing a competing product)_
- **Licence/EULA terms considered:** Alteryx EULA §3.3 restricts reverse engineering and use to develop competing products; the written authorisation is the basis for work that would otherwise fall under those restrictions. UK CDPA s.50A–50C noted as background.

### Operating rule while the scope fields are blank

Independent development from public specifications and original fixtures continues, together with the activities already in use (black-box differential execution, structural workflow-XML scan, static provenance hashing). **Held** until the covered activities are written down here: decompilation/disassembly (Ghidra, managed-metadata inspection), runtime instrumentation of vendor processes, and any network observation. Holding these is a record-keeping step, not doubt about the authorisation.

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
