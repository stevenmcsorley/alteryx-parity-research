# Linux and macOS desktop update

The shared React UI and Python tool registry are used on all desktop platforms. Native packaging now selects
Windows NSIS, Linux DEB/AppImage, and macOS app/DMG targets through Tauri's platform configuration files.
Windows retains its existing embedded-Python packaging script and DPAPI session format.

Linux and macOS use `desktop/scripts/package_unix_runtime.py` to stage uv-managed CPython 3.12.10 and engine
dependencies, including Python keyring for database passwords. The script tests imports after relocating the
runtime and runs the sales example using the relocated interpreter. It must run on the intended OS and CPU
architecture. See https://docs.astral.sh/uv/concepts/python-versions/ for the managed distribution source.

ChatGPT session tokens use macOS Keychain or Linux Secret Service through Rust keyring, with separate entries
for separate application data directories. Linux requires an unlocked desktop Secret Service (for example
GNOME Keyring) and a user D-Bus session. Vault failures return an error; tokens are never written to plaintext
files. See https://docs.rs/keyring/3.6.3/keyring/ for the native backend configuration.

## Native builds

Use a clean checkout, Python 3.12, Node 22, Rust stable, and uv. On Linux install GTK 3, WebKitGTK 4.1,
D-Bus development libraries, Ayatana AppIndicator, librsvg and patchelf as shown in
`.github/workflows/desktop-unix.yml`. macOS requires Xcode command-line tools.

```sh
python3 -m pip install -r requirements-dev.txt -r requirements-unix-runtime.txt
cd desktop
npm ci
cd ..
python3 desktop/scripts/package_unix_runtime.py
cd desktop
npm run tauri -- build
```

The native CI matrix builds Ubuntu x86_64 and macOS Apple Silicon packages and runs engine, frontend, and
Rust tests. Linux additionally extracts the DEB, launches its application with an isolated data directory,
verifies bundled-runtime discovery and the 323-entry catalogue, and checks a correlation workflow's CSV.
This is build automation; CI results and installed-app acceptance must be recorded before claiming release
readiness. macOS signing/notarization, macOS launch acceptance, Linux install/upgrade/uninstall acceptance,
and a macOS Intel package remain outstanding.

## Local validation

On the Linux development host: 237 Python tests ran without failure (one optional R test skipped), 46 frontend
tests passed, and 34 native Rust tests passed. Repository validation and evidence-ledger reproducibility passed.
The Linux debug DEB built successfully. Its extracted application launched using its own bundled Python,
loaded all 323 entries, and ran the correlation workflow with the expected four matrix records. The smoke
test is reproducible with:

```sh
python3 desktop/scripts/linux_package_smoke.py \
  desktop/src-tauri/target/debug/bundle/deb/Flowbench_0.2.0_amd64.deb \
  --evidence artifacts/linux-desktop-smoke
```

Use `xvfb-run -a` on headless Linux hosts. Local artifacts are debug/test builds; no macOS binary was built on
this Linux host and the new remote CI workflow has not yet been executed.

## Continued tool development

`core.pearson_correlation` adds a schema-configured tool available in the shared palette on every platform.
Select at least two numeric fields and choose Pearson correlation or sample covariance (n-1). Output is a
deterministic long-form matrix with `Field1`, `Field2`, and `Correlation` or `Covariance`. Constant-field
correlations are null; null/non-finite observations and fewer than two rows fail explicitly. No imputation is
implicit. This independently designed matrix output does not reproduce the vendor's report layout or claim
reference compatibility. Functional source:
https://help.alteryx.com/current/en/designer/tools/data-investigation/pearson-correlation-tool.html .

Tests exercise the registered tool through real workflows. The generated ledger records these independent
tests separately from differential reference tests; there are no new reference comparisons in this update.
