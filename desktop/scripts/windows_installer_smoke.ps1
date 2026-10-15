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
# Exercise the reported CSV -> Filter -> Browse case through the installed app's
# engine sidecar. The fixture explicitly declares text types so the expected
# lexical comparison is reproducible and distinguishes engine semantics from
# inferred-schema assumptions.
$AcceptanceRoot = Join-Path $env:RUNNER_TEMP 'Flowbench-data-correctness-filter'
New-Item -ItemType Directory -Path $AcceptanceRoot -Force | Out-Null
$InputCsv = Join-Path $AcceptanceRoot 'quantities.csv'
$TrueCsv = Join-Path $AcceptanceRoot 'filter-true.csv'
$FalseCsv = Join-Path $AcceptanceRoot 'filter-false.csv'
$workflowPath = Join-Path $AcceptanceRoot 'filter-workflow.flow.json'
$csvText = @'
Quantity,Sales
0,1.25
1,1.25
10,1.25
09,1.25
00123,1.25
-2,1.25
2,1.25
3,1.25
5,1.25
6,1.25
11,1.25
12,1.25
20,1.25
50,1.25
100,1.25
'@
$csvText = $csvText.TrimStart("`r", "`n") + "`n"
[IO.File]::WriteAllText($InputCsv, $csvText, [Text.UTF8Encoding]::new($false))
$workflow = @{
  format = 'flowengine.workflow'
  formatVersion = 1
  name = 'Installed desktop CSV Filter type acceptance'
  nodes = @(
    @{ id = 'input'; tool = 'core.input_data'; config = @{ path = $InputCsv; empty_as_null = $true; fields = @(
      @{ name = 'Quantity'; type = 'v_wstring' }, @{ name = 'Sales'; type = 'v_wstring' }) } },
    @{ id = 'filter'; tool = 'core.filter'; config = @{ field = 'Quantity'; operator = '>'; value = '10' } },
    @{ id = 'browse_true'; tool = 'core.browse'; config = @{} },
    @{ id = 'browse_false'; tool = 'core.browse'; config = @{} },
    @{ id = 'write_true'; tool = 'core.output_data'; config = @{ path = $TrueCsv; format = 'csv'; if_exists = 'overwrite' } },
    @{ id = 'write_false'; tool = 'core.output_data'; config = @{ path = $FalseCsv; format = 'csv'; if_exists = 'overwrite' } }
  )
  edges = @(
    @{ from = @{ node = 'input'; anchor = 'output' }; to = @{ node = 'filter'; anchor = 'input' } },
    @{ from = @{ node = 'filter'; anchor = 'true' }; to = @{ node = 'browse_true'; anchor = 'input' } },
    @{ from = @{ node = 'filter'; anchor = 'true' }; to = @{ node = 'write_true'; anchor = 'input' } },
    @{ from = @{ node = 'filter'; anchor = 'false' }; to = @{ node = 'browse_false'; anchor = 'input' } },
    @{ from = @{ node = 'filter'; anchor = 'false' }; to = @{ node = 'write_false'; anchor = 'input' } }
  )
}
$workflow | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $workflowPath -Encoding utf8
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
  if (-not (Test-Path -LiteralPath $TrueCsv) -or -not (Test-Path -LiteralPath $FalseCsv)) {
    throw 'Installed desktop CSV Filter workflow did not produce both anchor exports.'
  }
  $trueRows = @(Import-Csv -LiteralPath $TrueCsv)
  $falseRows = @(Import-Csv -LiteralPath $FalseCsv)
  if ($trueRows.Count -ne 9 -or $falseRows.Count -ne 6) {
    throw "Installed desktop Filter expected 9 true and 6 false string rows; got $($trueRows.Count) and $($falseRows.Count)."
  }
  if ($trueRows.Quantity -notcontains '2' -or $trueRows.Quantity -notcontains '100' -or
      $falseRows.Quantity -notcontains '00123' -or $falseRows.Quantity -notcontains '-2') {
    throw 'Installed desktop Filter output values do not match the expected lexical string comparison.'
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
Write-Host 'Clean-runner install, managed runtime launch, CSV -> Filter -> Browse (9/6 lexical results), same-version reinstall, and uninstall passed.'
