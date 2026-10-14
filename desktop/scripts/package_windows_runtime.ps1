$ErrorActionPreference = 'Stop'
$Version = '3.12.10'
$Runtime = Join-Path $PSScriptRoot '..\src-tauri\runtime'
$Runtime = [IO.Path]::GetFullPath($Runtime)
$Archive = Join-Path $env:RUNNER_TEMP "python-$Version-embed-amd64.zip"
$Stage = Join-Path $env:RUNNER_TEMP "flowbench-python-$Version"

if (Test-Path $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
Get-ChildItem -LiteralPath $Runtime -Force | Where-Object { $_.Name -ne '.gitkeep' } |
  Remove-Item -Recurse -Force
Invoke-WebRequest "https://www.python.org/ftp/python/$Version/python-$Version-embed-amd64.zip" -OutFile $Archive
Expand-Archive -LiteralPath $Archive -DestinationPath $Stage
$SetupPythonVersion = python -c 'import platform; print(platform.python_version())'
if ($LASTEXITCODE -ne 0 -or $SetupPythonVersion.Trim() -ne $Version) {
  throw "Build Python must be exactly $Version to match the bundled runtime; found $SetupPythonVersion."
}
$SetupPythonStdlib = python -c 'import sysconfig; print(sysconfig.get_path("stdlib"))'
if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $SetupPythonStdlib 'venv'))) {
  throw 'Could not locate the pinned build Python standard-library venv modules.'
}
New-Item -ItemType Directory -Force -Path (Join-Path $Stage 'Lib') | Out-Null
Copy-Item -LiteralPath (Join-Path $SetupPythonStdlib 'venv') -Destination (Join-Path $Stage 'Lib\venv') -Recurse -Force
Copy-Item -LiteralPath (Join-Path $SetupPythonStdlib 'ensurepip') -Destination (Join-Path $Stage 'Lib\ensurepip') -Recurse -Force
New-Item -ItemType Directory -Force -Path (Join-Path $Stage 'Lib\site-packages') | Out-Null
$PythonPath = @('python312.zip', '.', 'Lib', 'Lib\site-packages', '..\engine', 'import site')
[IO.File]::WriteAllLines((Join-Path $Stage 'python312._pth'), $PythonPath, [Text.Encoding]::ASCII)
# Metaphone 0.6 is distributed as source only. Build a pinned pure-Python wheel
# before the otherwise binary-only isolated Windows runtime installation.
$Wheelhouse = Join-Path $env:RUNNER_TEMP 'flowbench-runtime-wheels'
New-Item -ItemType Directory -Force -Path $Wheelhouse | Out-Null
python -m pip wheel --disable-pip-version-check --no-deps --no-binary=Metaphone `
  --wheel-dir $Wheelhouse 'Metaphone==0.6'
if ($LASTEXITCODE -ne 0) { throw 'Failed to build the pinned Metaphone runtime wheel.' }
python -m pip install --only-binary=:all: --no-compile --disable-pip-version-check `
  --find-links $Wheelhouse `
  --requirement (Join-Path $PSScriptRoot '..\..\requirements-windows-runtime.txt') `
  --target (Join-Path $Stage 'Lib\site-packages')
if ($LASTEXITCODE -ne 0) { throw 'Bundled Windows runtime dependency installation failed.' }
Copy-Item -Path (Join-Path $Stage '*') -Destination $Runtime -Recurse -Force
$BundledPython = Join-Path $Runtime 'python.exe'
& $BundledPython -c "import ensurepip, jsonschema, numpy, pyarrow, psycopg, pymssql, venv, yxdb; from metaphone import doublemetaphone; assert doublemetaphone('Smith')[0]; print('bundled runtime imports passed')"
if ($LASTEXITCODE -ne 0) { throw 'Bundled Python dependency smoke test failed.' }
$VenvSmoke = Join-Path $env:RUNNER_TEMP 'flowbench-venv-smoke'
if (Test-Path $VenvSmoke) { Remove-Item -LiteralPath $VenvSmoke -Recurse -Force }
& $BundledPython -m venv $VenvSmoke
if ($LASTEXITCODE -ne 0) { throw 'Bundled runtime could not create an isolated Python environment.' }
& (Join-Path $VenvSmoke 'Scripts\python.exe') -m pip --version
if ($LASTEXITCODE -ne 0) { throw 'Bundled runtime virtual environment did not provision pip.' }
