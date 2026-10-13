$ErrorActionPreference = 'Stop'
$Bundle = Join-Path $PSScriptRoot '..\src-tauri\target\release\bundle\nsis'
$Installer = Get-ChildItem -LiteralPath $Bundle -Filter '*-setup.exe' | Select-Object -First 1
if (-not $Installer) { throw "No NSIS installer was produced in $Bundle" }
$InstallDir = Join-Path $env:RUNNER_TEMP 'Flowbench-clean-install'
if (Test-Path $InstallDir) { Remove-Item -LiteralPath $InstallDir -Recurse -Force }
$env:PATH = @("$env:SystemRoot\System32", $env:SystemRoot, "$env:SystemRoot\System32\Wbem") -join ';'

function Install-Flowbench {
  $p = Start-Process -FilePath $Installer.FullName -ArgumentList @('/S', "/D=$InstallDir") -Wait -PassThru
  if ($p.ExitCode -ne 0) { throw "NSIS installation failed with exit code $($p.ExitCode)" }
}

Install-Flowbench
$App = Get-ChildItem -LiteralPath $InstallDir -Filter 'flowbench.exe' -File -Recurse |
  Select-Object -First 1 -ExpandProperty FullName
$Python = Get-ChildItem -LiteralPath $InstallDir -Filter 'python.exe' -File -Recurse |
  Where-Object { $_.FullName -match '[\\/]runtime[\\/]python\.exe$' } |
  Select-Object -First 1 -ExpandProperty FullName
if (-not $App -or -not $Python) {
  $tree = Get-ChildItem -LiteralPath $InstallDir -Force -Recurse | Select-Object -ExpandProperty FullName
  throw "Installed app or managed Python runtime is missing under $InstallDir. Contents: $($tree -join '; ')"
}
$ResourcesDir = Split-Path (Split-Path $Python -Parent) -Parent
$EngineRoot = Join-Path $ResourcesDir 'engine'
if (-not (Test-Path (Join-Path $EngineRoot 'flowengine\__main__.py'))) {
  throw "The packaged Flowbench engine is missing from $EngineRoot."
}
Push-Location $EngineRoot
try { & $Python -m flowengine --help | Out-Null } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw 'Bundled engine failed to start without PATH Python.' }
& $Python -c 'import jsonschema, numpy, pyarrow, psycopg, pymssql, yxdb'
if ($LASTEXITCODE -ne 0) { throw 'Installed runtime failed native dependency imports.' }
$workflowPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\examples\sales\monthly_sales.flow.json'))
$env:FLOWBENCH_STARTUP_SMOKE_WORKFLOW = $workflowPath
$process = Start-Process -FilePath $App -WorkingDirectory $InstallDir -PassThru
try {
  $startupLog = $null
  $logText = ''
  for ($i = 0; $i -lt 90 -and $logText -notmatch 'workflow\.run succeeded'; $i++) {
    Start-Sleep -Milliseconds 500
    $startupLog = Get-ChildItem -Path @($env:APPDATA, $env:LOCALAPPDATA) -Filter 'engine-startup.log' -File -Recurse -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($startupLog) { $logText = Get-Content -LiteralPath $startupLog.FullName -Raw }
  }
  if ($logText -notmatch 'engine\.info succeeded; catalog\.list entries=323; pid=(\d+)' -or
      $logText -notmatch 'workflow\.run succeeded') {
    $appState = Get-Process -Id $process.Id -ErrorAction SilentlyContinue | Select-Object Id,HasExited,Path
    throw "Installed app engine RPC/workflow health failed. App: $($appState | ConvertTo-Json -Compress); startup log: $($startupLog.FullName); contents: $logText"
  }
  $enginePid = [int]([regex]::Match($logText, 'engine\.info succeeded; catalog\.list entries=323; pid=(\d+)').Groups[1].Value)
  if (-not (Get-Process -Id $enginePid -ErrorAction SilentlyContinue)) { throw "Engine health RPC passed but sidecar PID $enginePid is no longer running." }
} finally {
  Remove-Item Env:FLOWBENCH_STARTUP_SMOKE_WORKFLOW -ErrorAction SilentlyContinue
  if (-not $process.HasExited) {
    [void]$process.CloseMainWindow()
    if (-not $process.WaitForExit(10000)) { Stop-Process -Id $process.Id -Force }
  }
}
Install-Flowbench # same-version reinstall exercises NSIS upgrade-in-place handling
$Uninstaller = Join-Path $InstallDir 'uninstall.exe'
if (-not (Test-Path $Uninstaller)) { throw 'NSIS uninstaller is missing.' }
$uninstall = Start-Process -FilePath $Uninstaller -ArgumentList '/S' -Wait -PassThru
if ($uninstall.ExitCode -ne 0 -or (Test-Path -LiteralPath $App)) { throw 'Silent uninstall did not remove the installed application.' }
Write-Host 'Clean-runner install, managed runtime launch, same-version reinstall, and uninstall passed.'
