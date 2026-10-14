[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $env:TEMP 'Flowbench-Action-Acceptance')
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'filter_update_app.xml'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Fixture source is missing: $source"
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$destination = Join-Path $OutputDirectory 'flowbench_filter_update_app.yxwz'
Copy-Item -LiteralPath $source -Destination $destination -Force
[xml]$check = Get-Content -LiteralPath $destination -Raw
if ($check.AlteryxDocument.Properties.RuntimeProperties.Actions.NoCondition.True.Action.Destination -ne '4/Expression') {
    throw 'Generated fixture failed its Action destination integrity check.'
}
Write-Host 'Synthetic Designer Action acceptance fixture prepared.' -ForegroundColor Green
Write-Host "Open this file in Alteryx Designer 2026.2.0.8: $destination"
Write-Host 'No credentials or secrets are contained in this fixture.'
