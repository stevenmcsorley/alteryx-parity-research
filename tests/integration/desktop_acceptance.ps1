param(
  [ValidateSet('Up', 'Status', 'Down', 'Mutate', 'Restore', 'CopyReaderPassword')]
  [string]$Action = 'Status',
  [ValidateSet('postgresql', 'sqlserver')]
  [string]$Database,
  [string]$ProjectRoot = (Join-Path $PSScriptRoot '..\..')
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$EnvFile = Join-Path $ProjectRoot 'tests\integration\.db_env.json'
$FixtureScript = Join-Path $ProjectRoot 'tests\integration\db_env.py'

function Invoke-FixturePython([string]$Code) {
  $env:FLOWBENCH_ACCEPTANCE_ENV = $EnvFile
  try {
    $Code | & python -
    if ($LASTEXITCODE -ne 0) { throw 'Fixture operation failed. See the safe status above; no password was printed.' }
  } finally {
    Remove-Item Env:FLOWBENCH_ACCEPTANCE_ENV -ErrorAction SilentlyContinue
  }
}

function Invoke-Status {
  if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Fixture settings are missing. Run: .\desktop_acceptance.ps1 -Action Up"
  }
  $code = @'
import json, os
from decimal import Decimal
from pathlib import Path
try:
    import psycopg, pymssql
except Exception:
    print('Missing Python database drivers. From the repository root run: python -m pip install -r requirements.txt')
    raise SystemExit(2)
c = json.loads(Path(os.environ['FLOWBENCH_ACCEPTANCE_ENV']).read_text(encoding='utf-8'))
pg = c['postgresql']; pgr = pg['reader']
try:
    with psycopg.connect(host='localhost', port=pg['port'], dbname=pg['database'], user=pgr['user'],
                         password=pgr['password'], sslmode='verify-full', sslrootcert=pg['ca_file'],
                         connect_timeout=5) as db:
        with db.cursor() as cur:
            cur.execute('SELECT count(*) FROM sales.customers')
            customers = cur.fetchone()[0]
    if customers != 41: raise RuntimeError('PostgreSQL fixture count mismatch')
except Exception as e:
    print(f'PostgreSQL readiness failed ({type(e).__name__}); check Docker, the CA file, and fixture status.')
    raise SystemExit(3)
ms = c['sqlserver']; msr = ms['reader']
try:
    with pymssql.connect(server=ms['host'], port=str(ms['port']), user=msr['user'], password=msr['password'],
                         database=ms['database'], login_timeout=5, tds_version='7.4') as db:
        cur = db.cursor()
        cur.execute('SELECT count(*) FROM ops.orders')
        orders = cur.fetchone()[0]
        cur.execute('SELECT customer_id, qty, unit_price FROM ops.orders WHERE order_id = 1007')
        mutation = cur.fetchone()
    mutation_price = Decimal(str(mutation[2])) if mutation is not None else None
    if orders != 120 or mutation is None or (mutation[0], mutation[1]) != (5, 1) or mutation_price not in (Decimal('21.29'), Decimal('22.29')):
        raise RuntimeError('SQL Server fixture values mismatch')
except Exception as e:
    print(f'SQL Server readiness failed ({type(e).__name__}); check Docker and fixture status.')
    raise SystemExit(4)
print(f'PostgreSQL: ready; TLS certificate and hostname verified; {customers} sales.customers rows.')
print(f'SQL Server: ready on loopback; {orders} ops.orders rows; order 1007 unit_price={mutation_price:.2f}.')
print('Reader logins verified. Generated passwords were not displayed.')
'@
  Invoke-FixturePython $code

  $cfg = Get-Content -LiteralPath $EnvFile -Raw | ConvertFrom-Json
  Write-Host ''
  Write-Host 'Use these values in Flowbench > Databases. Passwords are intentionally not shown.'
  Write-Host ('PostgreSQL: host=localhost port={0} database={1} user=fb_reader TLS=verify-full' -f $cfg.postgresql.port, $cfg.postgresql.database)
  Write-Host ('  Schema/table=sales.customers; CA certificate={0}; connect timeout=10s; query timeout=120s; writes=disabled' -f $cfg.postgresql.ca_file)
  Write-Host ('SQL Server: host=127.0.0.1 port={0} database={1} user=fb_reader TLS=disable (loopback fixture)' -f $cfg.sqlserver.port, $cfg.sqlserver.database)
  Write-Host '  Schema/table=ops.orders; connect timeout=10s; query timeout=120s; writes=disabled'
  Write-Host 'To copy one reader password without displaying it, use -Action CopyReaderPassword -Database postgresql|sqlserver.'
}

Push-Location $ProjectRoot
try {
  switch ($Action) {
    'Up' {
      if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker CLI is not installed or not on PATH.' }
      if (-not (Get-Command openssl -ErrorAction SilentlyContinue)) {
        $gitOpenSSL = @(
          (Join-Path $env:ProgramFiles 'Git\usr\bin\openssl.exe'),
          (Join-Path $env:ProgramFiles 'Git\mingw64\bin\openssl.exe')
        ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $gitOpenSSL) { throw 'OpenSSL is required by the existing PostgreSQL TLS fixture (Git for Windows includes it).'}
        $env:PATH = "$(Split-Path -Parent $gitOpenSSL);$env:PATH"
      }
      & docker info *> $null
      if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop is not running or the Docker engine is unavailable.' }
      $existing = @()
      foreach ($container in @('flowbench-test-pg', 'flowbench-test-mssql')) {
        & docker container inspect $container *> $null
        if ($LASTEXITCODE -eq 0) { $existing += $container }
      }
      if ($existing.Count) {
        $names = $existing -join ', '
        $answer = Read-Host "db_env.py up will replace the named synthetic test container(s): $names. Type REPLACE FIXTURES to continue"
        if ($answer -cne 'REPLACE FIXTURES') { throw 'Fixture startup cancelled; existing test containers were left alone.' }
      }
      if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Python 3.12 with repository requirements installed is needed to provision these existing fixtures.' }
      & python -c 'import psycopg, pymssql'
      if ($LASTEXITCODE -ne 0) { throw 'Python database drivers are missing. From the repository root run: python -m pip install -r requirements.txt' }
      & python $FixtureScript up
      if ($LASTEXITCODE -ne 0) { throw 'db_env.py up failed.' }
      Invoke-Status
    }
    'Status' { Invoke-Status }
    'Down' {
      if (-not (Test-Path -LiteralPath $EnvFile)) { Write-Host 'No fixture settings found; nothing to remove.'; break }
      $answer = Read-Host "This removes only '$($ProjectRoot)\tests\integration\$((Split-Path $EnvFile -Leaf))', containers flowbench-test-pg/flowbench-test-mssql, and their temporary PostgreSQL test certificate/private key. Type REMOVE FIXTURES to continue"
      if ($answer -cne 'REMOVE FIXTURES') { throw 'Fixture removal cancelled.' }
      $caFile = (Get-Content -LiteralPath $EnvFile -Raw | ConvertFrom-Json).postgresql.ca_file
      & python $FixtureScript down
      if ($LASTEXITCODE -ne 0) { throw 'db_env.py down failed.' }
      $certDir = Split-Path -Parent $caFile
      $tempPrefix = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
      $resolvedCertDir = [IO.Path]::GetFullPath($certDir)
      if ($caFile -and $caFile -like '*\server.crt' -and
          $resolvedCertDir.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
          (Split-Path -Leaf $resolvedCertDir) -like 'fb-pg-certs-*' -and
          (Test-Path -LiteralPath (Join-Path $resolvedCertDir 'server.key'))) {
        Remove-Item -LiteralPath $resolvedCertDir -Recurse -Force
        Write-Host 'Removed the temporary PostgreSQL test certificate and private key.'
      }
    }
    'CopyReaderPassword' {
      if (-not $Database) { throw 'Specify -Database postgresql or -Database sqlserver.' }
      if (-not (Test-Path -LiteralPath $EnvFile)) { throw 'Fixture settings are missing. Run -Action Up first.' }
      $code = @'
import json, os
from pathlib import Path
c=json.loads(Path(os.environ['FLOWBENCH_ACCEPTANCE_ENV']).read_text(encoding='utf-8'))
print(c[os.environ['FLOWBENCH_ACCEPTANCE_DATABASE']]['reader']['password'], end='')
'@
      $env:FLOWBENCH_ACCEPTANCE_DATABASE = $Database
      $env:FLOWBENCH_ACCEPTANCE_ENV = $EnvFile
      try {
        $secret = $code | & python -
        if ($LASTEXITCODE -ne 0 -or -not $secret) { throw 'Could not retrieve the selected fixture reader credential.' }
        Set-Clipboard -Value ([string]$secret)
        $secret = $null
        Write-Host 'Reader password copied to the Windows clipboard; paste it into the matching Flowbench password field, then clear the clipboard with Set-Clipboard -Value "".'
      } finally {
        $secret = $null
        Remove-Item Env:FLOWBENCH_ACCEPTANCE_ENV -ErrorAction SilentlyContinue
        Remove-Item Env:FLOWBENCH_ACCEPTANCE_DATABASE -ErrorAction SilentlyContinue
      }
    }
    { $_ -in 'Mutate', 'Restore' } {
      if (-not (Test-Path -LiteralPath $EnvFile)) { throw 'Fixture settings are missing. Run -Action Up first.' }
      $target = if ($Action -eq 'Mutate') { '21.29' } else { '22.29' }
      $value = if ($Action -eq 'Mutate') { '22.29' } else { '21.29' }
      $code = @'
import json, os, sys
from decimal import Decimal
from pathlib import Path
import pymssql
c=json.loads(Path(os.environ['FLOWBENCH_ACCEPTANCE_ENV']).read_text(encoding='utf-8'))['sqlserver']
try:
    db=pymssql.connect(server=c['host'], port=str(c['port']), user='sa', password=c['admin_password'],
                       database=c['database'], login_timeout=5, autocommit=False, tds_version='7.4')
    cur=db.cursor()
    cur.execute('SELECT unit_price FROM ops.orders WHERE order_id = 1007')
    row=cur.fetchone()
    if row is None: raise RuntimeError('fixture order is missing')
    current=Decimal(str(row[0]))
    old=Decimal(os.environ['FLOWBENCH_ACCEPTANCE_EXPECTED'])
    new=Decimal(os.environ['FLOWBENCH_ACCEPTANCE_VALUE'])
    if current == new:
        db.rollback(); print(f'Order 1007 is already at the requested safe fixture value {new:.2f}.')
    elif current == old:
        cur.execute('UPDATE ops.orders SET unit_price = %s WHERE order_id = %s AND unit_price = %s',
                    (new, 1007, old))
        if cur.rowcount != 1: raise RuntimeError('guarded update did not affect exactly one row')
        db.commit(); print(f'Updated synthetic order 1007 unit_price from {old:.2f} to {new:.2f}.')
    else:
        db.rollback(); raise RuntimeError(f'order 1007 has unexpected unit_price {current}; no update made')
except Exception as e:
    try: db.rollback()
    except Exception: pass
    print(f'Fixture mutation failed ({type(e).__name__}); no unguarded update was made.')
    raise SystemExit(5)
finally:
    try: db.close()
    except Exception: pass
'@
      $env:FLOWBENCH_ACCEPTANCE_EXPECTED = $target
      $env:FLOWBENCH_ACCEPTANCE_VALUE = $value
      try { Invoke-FixturePython $code }
      finally {
        Remove-Item Env:FLOWBENCH_ACCEPTANCE_EXPECTED, Env:FLOWBENCH_ACCEPTANCE_VALUE -ErrorAction SilentlyContinue
      }
    }
  }
} finally {
  Remove-Item Env:FLOWBENCH_ACCEPTANCE_DATABASE -ErrorAction SilentlyContinue
  Pop-Location
}
