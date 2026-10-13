<#!
.SYNOPSIS
  Make a metadata-only inventory of operator-authorised locally installed files.
.DESCRIPTION
  No file content, application binaries or software artefacts are copied.
  Use only for installations you are entitled to inventory. Output defaults
  to the OS temp directory (outside source control) and may contain private paths.
#>
param(
  [Parameter(Mandatory = $true)][string]$TargetDirectory,
  [string]$OutputPath = (Join-Path $env:TEMP 'designer-reference-inventory.json')
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $TargetDirectory).Path
$extensions = @('.exe','.dll','.yxmd','.yxmc','.yxwz','.yxzp','.yxi','.config','.json')
$files = Get-ChildItem -LiteralPath $root -File -Recurse -ErrorAction Stop |
  Where-Object { $extensions -contains $_.Extension.ToLowerInvariant() } |
  ForEach-Object {
    [pscustomobject]@{
      relativePath = $_.FullName.Substring($root.Length).TrimStart('\')
      extension = $_.Extension
      bytes = $_.Length
      sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
      lastWriteUtc = $_.LastWriteTimeUtc.ToString('o')
    }
  }
$report = [pscustomobject]@{
  createdUtc = [DateTime]::UtcNow.ToString('o')
  sourceDirectory = $root
  warning = 'Metadata and hashes only. Do not commit: paths can be sensitive.'
  files = @($files)
}
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
Write-Host "Inventory saved to $OutputPath. Do NOT commit it."
