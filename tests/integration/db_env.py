#!/usr/bin/env python3
"""Isolated, real database servers for integration tests (Docker): PostgreSQL 16 with TLS and SQL Server 2022.

    python tests/integration/db_env.py up      # start + load fixtures, writes tests/integration/.db_env.json
    python tests/integration/db_env.py down    # remove containers

Each run uses random ports and freshly generated passwords. Logins:
  * fb_reader  - SELECT only (used by read tests and the demo workflow)
  * fb_writer  - may create/insert in schema "scratch" only (write tests)
Fixtures are deterministic synthetic data (no real people or organisations).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / '.db_env.json'
PG_IMAGE = os.environ.get('FLOWBENCH_PG_IMAGE', 'postgres:16')
MS_IMAGE = os.environ.get('FLOWBENCH_MSSQL_IMAGE', 'mcr.microsoft.com/mssql/server:2022-CU14-ubuntu-22.04')
PG_NAME, MS_NAME = 'flowbench-test-pg', 'flowbench-test-mssql'
BIG_ROWS = 200_000


def sh(*args, check=True, **kw):
    return subprocess.run(list(args), check=check, capture_output=True, text=True, **kw)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def pw():
    return 'Fb' + secrets.token_urlsafe(18).replace('-', 'x').replace('_', 'y') + '9!'


# --------------------------------------------------------------------------- fixtures (deterministic)

FIRST = ['Ada', 'Ben', 'Chidi', 'Dana', 'Eli', 'Fen', 'Gus', 'Hana', 'Ivo', 'Jun', 'Kai', 'Lea', 'Mo', 'Noor', 'Oli']
REGIONS = ['North', 'South', 'East', 'West']


def customers():
    rows = []
    for i in range(1, 41):
        rows.append((i, f'{FIRST[i % len(FIRST)]} {chr(65 + i % 26)}.', REGIONS[i % 4] if i % 11 else None,
                     round(10 + (i * 37 % 900) / 7, 2) if i % 9 else None,
                     dt.date(2024, 1, 1) + dt.timedelta(days=i * 9), i % 3 != 0))
    rows.append((41, 'Zoë Ünïcode', 'North', 5.5, dt.date(2025, 2, 28), True))
    return rows


def orders():
    rows = []
    for i in range(1, 121):
        cust = (i * 7) % 45 + 1  # includes ids 42-45 with no customer
        rows.append((1000 + i, cust, dt.date(2025, 1, 1) + dt.timedelta(days=i % 90), (i * 13) % 7 + 1,
                     round(((i * 29) % 500) / 10 + 0.99, 2), 'web' if i % 3 else ('store' if i % 2 else None)))
    return rows


# --------------------------------------------------------------------------- PostgreSQL

def pg_up(cfg):
    cert_dir = Path(tempfile.mkdtemp(prefix='fb-pg-certs-'))
    sh('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2', '-subj', '/CN=localhost',
       '-keyout', str(cert_dir / 'server.key'), '-out', str(cert_dir / 'server.crt'))
    port, admin = free_port(), pw()
    sh('docker', 'rm', '-f', PG_NAME, check=False)
    entry = ('cp /certs/server.crt /certs/server.key /var/lib/postgresql/ && '
             'chown postgres:postgres /var/lib/postgresql/server.* && chmod 600 /var/lib/postgresql/server.key && '
             'exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/var/lib/postgresql/server.crt '
             '-c ssl_key_file=/var/lib/postgresql/server.key')
    sh('docker', 'run', '-d', '--name', PG_NAME, '-e', f'POSTGRES_PASSWORD={admin}', '-e', 'POSTGRES_DB=flowbench_test',
       '-p', f'127.0.0.1:{port}:5432', '-v', f'{cert_dir}:/certs:ro', '--entrypoint', 'bash', PG_IMAGE, '-c', entry)
    import psycopg
    deadline = time.time() + 120
    while True:
        try:
            conn = psycopg.connect(host='127.0.0.1', port=port, user='postgres', password=admin, dbname='flowbench_test',
                                   connect_timeout=3, autocommit=True)
            break
        except psycopg.OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(1)
    reader, writer = pw(), pw()
    with conn:
        c = conn.cursor()
        c.execute('CREATE SCHEMA sales')
        c.execute('CREATE SCHEMA scratch')
        c.execute('CREATE TABLE sales.customers (customer_id integer PRIMARY KEY, name varchar(40) NOT NULL, '
                  'region varchar(10), credit numeric(10,2), signup_date date, active boolean)')
        c.executemany('INSERT INTO sales.customers VALUES (%s,%s,%s,%s,%s,%s)', customers())
        c.execute('CREATE VIEW sales.active_customers AS SELECT * FROM sales.customers WHERE active')
        c.execute('CREATE TABLE sales.join_cases (seq integer PRIMARY KEY, k text, amount numeric(8,2))')
        c.executemany('INSERT INTO sales.join_cases VALUES (%s,%s,%s)',
                      [(1, 'A', 1.25), (2, 'a', 2.50), (3, None, 3.75), (4, None, 4.00),
                       (5, 'É', 5.25), (6, 'é', 6.50)])
        c.execute('CREATE TABLE sales.type_matrix (id integer PRIMARY KEY, i16 smallint, i64 bigint, f4 real, '
                  'f8 double precision, dec numeric(12,4), txt text, ts timestamp, tstz timestamptz, '
                  'tm time, bin bytea, flag boolean, uid uuid)')
        c.execute("INSERT INTO sales.type_matrix VALUES (1, -32768, 9223372036854775807, 1.5, 0.1, 12345678.1234, "
                  "'zoë ✓', '2024-02-29 13:45:06.5', '2024-06-01 12:00:00+02', '23:59:58', '\\x00ff10', true, "
                  "'6f9619ff-8b86-d011-b42d-00c04fc964ff'), (2, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
                  "NULL, NULL, NULL, NULL)")
        c.execute(f'CREATE TABLE sales.big AS SELECT g AS id, (g % 97) AS bucket, (g * 7 % 1000) / 10.0 AS amount, '
                  f"'row-' || g AS label FROM generate_series(1, {BIG_ROWS}) g")
        c.execute('CREATE TABLE sales.live_counter (id integer PRIMARY KEY, value integer)')
        c.execute('INSERT INTO sales.live_counter VALUES (1, 1)')
        c.execute(f"CREATE ROLE fb_reader LOGIN PASSWORD '{reader}'")
        c.execute('GRANT USAGE ON SCHEMA sales TO fb_reader')
        c.execute('GRANT SELECT ON ALL TABLES IN SCHEMA sales TO fb_reader')
        c.execute(f"CREATE ROLE fb_writer LOGIN PASSWORD '{writer}'")
        c.execute('GRANT USAGE ON SCHEMA sales TO fb_writer')
        c.execute('GRANT SELECT ON ALL TABLES IN SCHEMA sales TO fb_writer')
        c.execute('GRANT USAGE, CREATE ON SCHEMA scratch TO fb_writer')
        c.execute('GRANT SELECT, INSERT, UPDATE ON sales.live_counter TO fb_writer')
    conn.close()
    cfg['postgresql'] = {'host': '127.0.0.1', 'port': port, 'database': 'flowbench_test', 'admin_password': admin,
                         'reader': {'user': 'fb_reader', 'password': reader},
                         'writer': {'user': 'fb_writer', 'password': writer}, 'ca_file': str(cert_dir / 'server.crt')}


# --------------------------------------------------------------------------- SQL Server

def ms_up(cfg):
    port, admin = free_port(), pw()
    sh('docker', 'rm', '-f', MS_NAME, check=False)
    sh('docker', 'run', '-d', '--name', MS_NAME, '-e', 'ACCEPT_EULA=Y', '-e', f'MSSQL_SA_PASSWORD={admin}',
       '-e', 'MSSQL_PID=Developer', '-p', f'127.0.0.1:{port}:1433', MS_IMAGE)
    import pymssql
    deadline = time.time() + 240
    while True:
        try:
            conn = pymssql.connect(server='127.0.0.1', port=str(port), user='sa', password=admin, login_timeout=5,
                                   autocommit=True, tds_version='7.4')
            break
        except pymssql.Error:
            if time.time() > deadline:
                raise
            time.sleep(2)
    reader, writer = pw(), pw()
    c = conn.cursor()
    c.execute('CREATE DATABASE flowbench_test')
    c.execute('USE flowbench_test')
    c.execute('EXEC(\'CREATE SCHEMA ops\')')
    c.execute('EXEC(\'CREATE SCHEMA scratch\')')
    c.execute('CREATE TABLE ops.orders (order_id int PRIMARY KEY, customer_id int NOT NULL, order_date date, '
              'qty smallint, unit_price decimal(9,2), channel nvarchar(10))')
    c.executemany('INSERT INTO ops.orders VALUES (%s,%s,%s,%s,%s,%s)', orders())
    c.execute('CREATE TABLE ops.join_cases (seq int PRIMARY KEY, k nvarchar(10), amount decimal(8,2))')
    c.executemany('INSERT INTO ops.join_cases VALUES (%s,%s,%s)',
                  [(1, 'A', 1.25), (2, 'a', 2.50), (3, None, 3.75), (4, None, 4.00),
                   (5, 'É', 5.25), (6, 'é', 6.50)])
    c.execute('CREATE TABLE ops.type_matrix (id int PRIMARY KEY, tiny tinyint, big bigint, r real, f float, '
              'd decimal(12,4), nv nvarchar(50), v varchar(20), dt2 datetime2, dto datetimeoffset, tm time, '
              'bin varbinary(10), flag bit, uid uniqueidentifier)')
    c.execute("INSERT INTO ops.type_matrix VALUES (1, 255, 9223372036854775807, 1.5, 0.1, 12345678.1234, N'zoë ✓', "
              "'plain', '2024-02-29 13:45:06.5', '2024-06-01 12:00:00 +02:00', '23:59:58', 0x00FF10, 1, "
              "'6F9619FF-8B86-D011-B42D-00C04FC964FF'), (2, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
              "NULL, NULL, NULL, NULL)")
    c.execute(f'WITH n AS (SELECT TOP ({BIG_ROWS}) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS g '
              'FROM sys.all_objects a CROSS JOIN sys.all_objects b) '
              "SELECT CAST(g AS int) AS id, CAST(g % 97 AS int) AS bucket, CAST((g * 7 % 1000) / 10.0 AS float) AS amount, "
              "CONCAT('row-', g) AS label INTO ops.big FROM n")
    c.execute('CREATE TABLE ops.live_counter (id int PRIMARY KEY, value int)')
    c.execute('INSERT INTO ops.live_counter VALUES (1, 1)')
    for login, secret, role in (('fb_reader', reader, 'reader'), ('fb_writer', writer, 'writer')):
        c.execute(f"CREATE LOGIN {login} WITH PASSWORD = '{secret}', CHECK_POLICY = OFF")
        c.execute(f'CREATE USER {login} FOR LOGIN {login}')
        c.execute(f'GRANT SELECT ON SCHEMA::ops TO {login}')
    c.execute('GRANT CREATE TABLE TO fb_writer')
    c.execute('GRANT ALTER, INSERT, SELECT ON SCHEMA::scratch TO fb_writer')
    c.execute('GRANT SELECT, INSERT, UPDATE ON ops.live_counter TO fb_writer')
    conn.close()
    cfg['sqlserver'] = {'host': '127.0.0.1', 'port': port, 'database': 'flowbench_test', 'admin_password': admin,
                        'reader': {'user': 'fb_reader', 'password': reader},
                        'writer': {'user': 'fb_writer', 'password': writer}}


def up():
    cfg = {'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'big_rows': BIG_ROWS}
    pg_up(cfg)
    ms_up(cfg)
    ENV_FILE.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
    print(f"PostgreSQL on {cfg['postgresql']['port']}, SQL Server on {cfg['sqlserver']['port']}; wrote {ENV_FILE.name}")


def down():
    for n in (PG_NAME, MS_NAME):
        sh('docker', 'rm', '-f', n, check=False)
    if ENV_FILE.exists():
        ENV_FILE.unlink()
    print('removed test databases')


if __name__ == '__main__':
    {'up': up, 'down': down}[sys.argv[1] if len(sys.argv) > 1 else 'up']()
