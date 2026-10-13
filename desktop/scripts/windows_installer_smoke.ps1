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
& $Python -m flowengine --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Bundled engine failed to start without PATH Python.' }
& $Python -c 'import jsonschema, numpy, pyarrow, psycopg, pymssql, yxdb'
if ($LASTEXITCODE -ne 0) { throw 'Installed runtime failed native dependency imports.' }
$process = Start-Process -FilePath $App -WorkingDirectory $InstallDir -PassThru
try {
  $sidecar = $null
  for ($i = 0; $i -lt 40 -and -not $sidecar; $i++) {
    Start-Sleep -Milliseconds 500
    $sidecar = Get-CimInstance Win32_Process | Where-Object {
      $_.Name -eq 'python.exe' -and $_.ExecutablePath -eq $Python -and $_.ParentProcessId -eq $process.Id
    } | Select-Object -First 1
  }
  if (-not $sidecar) { throw 'Desktop failed to launch the bundled engine sidecar with system runtimes removed from PATH.' }
} finally {
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
