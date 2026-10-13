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
New-Item -ItemType Directory -Force -Path (Join-Path $Stage 'Lib\site-packages') | Out-Null
$PythonPath = @('python312.zip', '.', 'Lib\site-packages', '..\engine', 'import site')
[IO.File]::WriteAllLines((Join-Path $Stage 'python312._pth'), $PythonPath, [Text.Encoding]::ASCII)
python -m pip install --only-binary=:all: --no-compile --disable-pip-version-check `
  --requirement (Join-Path $PSScriptRoot '..\..\requirements-windows-runtime.txt') `
  --target (Join-Path $Stage 'Lib\site-packages')
Copy-Item -Path (Join-Path $Stage '*') -Destination $Runtime -Recurse -Force
$BundledPython = Join-Path $Runtime 'python.exe'
& $BundledPython -c "import jsonschema, numpy, pyarrow, psycopg, pymssql, yxdb; print('bundled runtime imports passed')"
if ($LASTEXITCODE -ne 0) { throw 'Bundled Python dependency smoke test failed.' }
