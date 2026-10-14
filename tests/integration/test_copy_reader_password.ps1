$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$scriptUnderTest = Join-Path $PSScriptRoot 'desktop_acceptance.ps1'
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('flowbench-password-regression-' + [guid]::NewGuid().ToString('N'))
$envDir = Join-Path $tempRoot 'tests\integration'
New-Item -ItemType Directory -Force -Path $envDir | Out-Null
$envFile = Join-Path $envDir '.db_env.json'
$sentinel = 'regression-only-secret-do-not-print'
@{ postgresql = @{ reader = @{ password = $sentinel } } } |
  ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $envFile -Encoding utf8

function global:Set-Clipboard {
  param([string]$Value)
  $global:FlowbenchCapturedClipboard = $Value
}

try {
  & $scriptUnderTest -Action CopyReaderPassword -Database postgresql -ProjectRoot $tempRoot | Out-Null
  if ($global:FlowbenchCapturedClipboard -cne $sentinel) {
    throw 'CopyReaderPassword did not pass the selected password to the clipboard command.'
  }
  if (Test-Path Env:FLOWBENCH_ACCEPTANCE_ENV) {
    throw 'CopyReaderPassword left FLOWBENCH_ACCEPTANCE_ENV in the parent process.'
  }
  if (Test-Path Env:FLOWBENCH_ACCEPTANCE_DATABASE) {
    throw 'CopyReaderPassword left FLOWBENCH_ACCEPTANCE_DATABASE in the parent process.'
  }
  Write-Host 'PASS: CopyReaderPassword supplies the fixture path to Python and clears its environment.'
}
finally {
  Remove-Item Env:FLOWBENCH_ACCEPTANCE_ENV, Env:FLOWBENCH_ACCEPTANCE_DATABASE -ErrorAction SilentlyContinue
  Remove-Item Function:\Set-Clipboard -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
