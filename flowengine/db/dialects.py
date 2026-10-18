"""PostgreSQL and SQL Server dialects: connecting (TLS, timeouts), read-only sessions, catalogue browsing,
type mapping, typed batch fetching and cancellation.

Drivers: psycopg 3 (PostgreSQL) and pymssql (SQL Server, bundled FreeTDS with TLS). Both use the
``%s`` parameter style; SQL text produced here never interpolates values.
"""
from __future__ import annotations

import datetime as _dt
import re
import threading
import time
import uuid
from decimal import Decimal

from ..errors import CancelledError, ConfigError, DataError, EngineError
from ..types import Field, FieldType

TEXT = 2**30 - 1


class DatabaseError(EngineError):
    code = 'database-error'


class ConnectError(DatabaseError):
    code = 'database-connect-error'


class ReadOnlyViolation(DatabaseError):
    code = 'database-read-only'


# =========================================================================== read-only SQL guard

# Keywords that can change state from inside a statement that starts with SELECT/WITH (SELECT INTO, data-modifying
# CTEs, remote rowset functions). Second layer: PostgreSQL sessions are read-only on the server and SQL Server
# reads run in a transaction that is always rolled back.
_FORBIDDEN = {'INTO', 'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'DROP', 'ALTER', 'CREATE', 'TRUNCATE', 'GRANT', 'REVOKE',
              'EXEC', 'EXECUTE', 'OPENROWSET', 'OPENQUERY', 'OPENDATASOURCE', 'WAITFOR'}


def strip_sql(sql: str) -> str:
    """Remove comments, string literals and quoted identifiers (replaced by placeholders)."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith('--', i):
            j = sql.find('\n', i)
            i = n if j < 0 else j
        elif sql.startswith('/*', i):
            j = sql.find('*/', i + 2)
            if j < 0:
                raise ReadOnlyViolation('Unterminated comment in SQL')
            i = j + 2
            out.append(' ')
        elif c in "'\"[`":
            close = {'[': ']'}.get(c, c)
            j = i + 1
            while True:
                j = sql.find(close, j)
                if j < 0:
                    raise ReadOnlyViolation('Unterminated quoted text in SQL')
                if close != ']' and sql.startswith(close * 2, j):
                    j += 2
                    continue
                break
            out.append(' _q_ ')
            i = j + 1
        elif c == '$' and re.match(r'\$[A-Za-z_]*\$', sql[i:]):
            tag = re.match(r'\$[A-Za-z_]*\$', sql[i:]).group()
            j = sql.find(tag, i + len(tag))
            if j < 0:
                raise ReadOnlyViolation('Unterminated dollar-quoted text in SQL')
            out.append(' _q_ ')
            i = j + len(tag)
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def check_read_only_sql(sql: str) -> None:
    """Accept a single SELECT / WITH ... SELECT statement and nothing that can modify state.
    Defence in depth: sessions are also read-only on the server (PostgreSQL) or rolled back (SQL Server)."""
    body = strip_sql(sql).strip()
    body = body[:-1].rstrip() if body.endswith(';') else body
    if ';' in body:
        raise ReadOnlyViolation('Only a single statement is allowed')
    words = re.findall(r'[A-Za-z_]+', body.upper())
    if not words or words[0] not in ('SELECT', 'WITH', 'VALUES', 'TABLE'):
        raise ReadOnlyViolation('Only read-only queries (SELECT / WITH ... SELECT) are allowed on this connection')
    bad = sorted(set(words) & _FORBIDDEN)
    if bad:
        raise ReadOnlyViolation(f'Read-only connection: statement contains {", ".join(bad)}')


# =========================================================================== dialects

class Dialect:
    name = ''
    binary_collation = ''

    def __init__(self, conn_cfg, password):
        self.cfg = conn_cfg
        self.password = password

    # identifiers -----------------------------------------------------------
    def quote(self, ident: str) -> str:
        raise NotImplementedError

    def qualified(self, schema: str | None, table: str) -> str:
        return f'{self.quote(schema)}.{self.quote(table)}' if schema else self.quote(table)

    def _scrub(self, message: str) -> str:
        if self.password:
            message = message.replace(self.password, '********')
        return re.sub(r'(?i)(password|pwd)\s*=\s*\S+', r'\1=********', message)

    # connection ------------------------------------------------------------
    def connect(self, read_only=True):
        raise NotImplementedError

    def cancel(self, conn):
        raise NotImplementedError

    # catalogue ------------------------------------------------------------
    def schemas(self, conn) -> list[str]:
        raise NotImplementedError

    def tables(self, conn, schema) -> list[dict]:
        raise NotImplementedError

    def columns(self, conn, schema, table) -> list[Field]:
        raise NotImplementedError

    def describe_query(self, conn, sql, params) -> list[Field]:
        raise NotImplementedError

    def limit(self, sql: str, n: int) -> str:
        raise NotImplementedError


# ----------------------------------------------------------------------------- PostgreSQL

_PG_TYPES = {16: FieldType.BOOL, 20: FieldType.INT64, 21: FieldType.INT16, 23: FieldType.INT32, 26: FieldType.INT64,
             700: FieldType.FLOAT, 701: FieldType.DOUBLE, 1082: FieldType.DATE, 1083: FieldType.TIME,
             1114: FieldType.DATETIME, 1184: FieldType.DATETIME, 17: FieldType.BLOB}


class PostgreSQL(Dialect):
    name = 'postgresql'
    binary_collation = 'COLLATE "C"'

    def quote(self, ident):
        return '"' + ident.replace('"', '""').replace('%', '%%') + '"'

    def connect(self, read_only=True):
        import psycopg
        c = self.cfg
        sslmode = {'disable': 'disable', 'prefer': 'prefer', 'require': 'require', 'verify-full': 'verify-full'}[c.tls]
        kw = dict(host=c.host, port=int(c.port), dbname=c.database, user=c.user, password=self.password or None,
                  connect_timeout=int(c.connect_timeout), sslmode=sslmode, application_name='Flowbench')
        if c.ca_file:
            kw['sslrootcert'] = c.ca_file
        opts = []
        if read_only:
            opts.append('-c default_transaction_read_only=on')
        if c.query_timeout:
            opts.append(f'-c statement_timeout={int(c.query_timeout) * 1000}')
        if opts:
            kw['options'] = ' '.join(opts)
        try:
            conn = psycopg.connect(**kw)
        except psycopg.OperationalError as e:
            raise ConnectError(f'Could not connect to PostgreSQL {c.host}:{c.port}/{c.database}: '
                               f'{self._scrub(str(e)).strip()}') from None
        if read_only:
            conn.read_only = True
        return conn

    def cancel(self, conn):
        try:
            conn.cancel()
        except Exception:
            pass

    def run(self, conn, sql, params=()):
        cur = conn.cursor()
        cur.execute(sql, params or None)
        return cur

    def schemas(self, conn):
        cur = self.run(conn, "SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN "
                             "('pg_catalog','information_schema','pg_toast') AND schema_name NOT LIKE 'pg_temp%%' "
                             "AND schema_name NOT LIKE 'pg_toast_temp%%' ORDER BY schema_name")
        return [r[0] for r in cur.fetchall()]

    def tables(self, conn, schema):
        cur = self.run(conn, "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = %s "
                             "ORDER BY table_name", (schema,))
        return [{'name': n, 'kind': 'view' if t == 'VIEW' else 'table'} for n, t in cur.fetchall()]

    def describe_query(self, conn, sql, params):
        cur = conn.cursor()
        cur.execute(f'SELECT * FROM ({sql}) flowbench_describe LIMIT 0', params or None)
        return [self._field(d) for d in cur.description]

    def columns(self, conn, schema, table):
        return self.describe_query(conn, f'SELECT * FROM {self.qualified(schema, table)}', ())

    def _field(self, d) -> Field:
        oid = d.type_code
        if oid in _PG_TYPES:
            return Field(d.name, _PG_TYPES[oid])
        if oid == 1700:  # numeric
            if d.precision is not None and d.scale is not None and d.precision <= 50:
                return Field(d.name, FieldType.FIXED_DECIMAL, max(d.precision, 1), d.scale)
            return Field(d.name, FieldType.DOUBLE)
        if oid in (1043, 1042):  # varchar / char
            size = d.internal_size if d.internal_size and d.internal_size > 0 else TEXT
            return Field(d.name, FieldType.V_WSTRING, size)
        return Field(d.name, FieldType.V_WSTRING, TEXT)  # text, uuid, json, others rendered as text

    def convert(self, f: Field):
        if f.type is FieldType.DATETIME:
            return lambda v: None if v is None else (v.astimezone(_dt.timezone.utc).replace(tzinfo=None)
                                                     if isinstance(v, _dt.datetime) and v.tzinfo else v)
        if f.type is FieldType.TIME:
            return lambda v: None if v is None else v.replace(tzinfo=None)
        if f.type is FieldType.V_WSTRING:
            return lambda v: v if v is None or isinstance(v, str) else (str(v) if isinstance(v, uuid.UUID) else
                                                                        __import__('json').dumps(v))
        if f.type is FieldType.BLOB:
            return lambda v: None if v is None else bytes(v)
        if f.type is FieldType.DOUBLE:
            return lambda v: None if v is None else float(v)
        return None

    def limit(self, sql, n):
        return f'SELECT * FROM ({sql}) flowbench_limited LIMIT {int(n)}'


# ----------------------------------------------------------------------------- SQL Server

_MS_TYPES = {'bit': FieldType.BOOL, 'tinyint': FieldType.BYTE, 'smallint': FieldType.INT16, 'int': FieldType.INT32,
             'bigint': FieldType.INT64, 'real': FieldType.FLOAT, 'float': FieldType.DOUBLE, 'date': FieldType.DATE,
             'time': FieldType.TIME, 'datetime': FieldType.DATETIME, 'datetime2': FieldType.DATETIME,
             'smalldatetime': FieldType.DATETIME, 'datetimeoffset': FieldType.DATETIME, 'binary': FieldType.BLOB,
             'varbinary': FieldType.BLOB, 'image': FieldType.BLOB}


class SQLServer(Dialect):
    name = 'sqlserver'
    binary_collation = 'COLLATE Latin1_General_100_BIN2'

    def quote(self, ident):
        return '[' + ident.replace(']', ']]').replace('%', '%%') + ']'

    def connect(self, read_only=True):
        import pymssql
        c = self.cfg
        encryption = {'disable': 'off', 'prefer': 'request', 'require': 'require', 'verify-full': 'require'}[c.tls]
        if c.tls == 'verify-full':
            raise ConfigError('verify-full is not supported for SQL Server with the bundled driver; use "require"')
        try:
            conn = pymssql.connect(server=c.host, port=str(c.port), user=c.user, password=self.password or '',
                                   database=c.database, login_timeout=int(c.connect_timeout),
                                   timeout=int(c.query_timeout or 0), encryption=encryption, autocommit=False,
                                   appname='Flowbench', tds_version='7.4', use_datetime2=True)
        except pymssql.Error as e:
            raise ConnectError(f'Could not connect to SQL Server {c.host}:{c.port}/{c.database}: '
                               f'{self._scrub(_ms_message(e))}') from None
        return conn

    def cancel(self, conn):
        try:
            conn._conn.cancel()
        except Exception:
            pass

    def run(self, conn, sql, params=()):
        cur = conn.cursor()
        cur.execute(sql, tuple(params) if params else None)
        return cur

    def schemas(self, conn):
        cur = self.run(conn, "SELECT s.name FROM sys.schemas s WHERE s.name NOT IN ('sys','INFORMATION_SCHEMA','guest') "
                             "AND s.name NOT LIKE 'db[_]%%' ORDER BY s.name")
        return [r[0] for r in cur.fetchall()]

    def tables(self, conn, schema):
        cur = self.run(conn, "SELECT TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = %s "
                             "ORDER BY TABLE_NAME", (schema,))
        return [{'name': n, 'kind': 'view' if t == 'VIEW' else 'table'} for n, t in cur.fetchall()]

    def describe_query(self, conn, sql, params):
        # sp_describe_first_result_set gives exact types; parameters are declared as nvarchar for description
        n = sql.count('%s')
        tsql = sql
        decl = []
        for i in range(n):
            tsql = tsql.replace('%s', f'@p{i}', 1)
            decl.append(f'@p{i} nvarchar(4000)')
        tsql = tsql.replace('%%', '%')
        cur = conn.cursor()
        cur.execute('EXEC sp_describe_first_result_set @tsql = %s, @params = %s', (tsql, ', '.join(decl) or None))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        fields = []
        for r in rows:
            if r['is_hidden']:
                continue
            t = (r['system_type_name'] or '').split('(')[0].lower()
            name = r['name'] or f'Column{r["column_ordinal"]}'
            if t in _MS_TYPES:
                fields.append(Field(name, _MS_TYPES[t]))
            elif t in ('decimal', 'numeric', 'money', 'smallmoney'):
                p, s = int(r['precision'] or 19), int(r['scale'] or 4)
                fields.append(Field(name, FieldType.FIXED_DECIMAL, min(max(p, 1), 38), s) if p <= 50 else
                              Field(name, FieldType.DOUBLE))
            elif t in ('char', 'varchar', 'nchar', 'nvarchar', 'text', 'ntext', 'sysname', 'xml', 'uniqueidentifier'):
                ml = int(r['max_length'] or -1)
                size = TEXT if ml <= 0 else (ml // 2 if t.startswith('n') or t == 'sysname' else ml)
                if t == 'uniqueidentifier':
                    size = 36
                fields.append(Field(name, FieldType.V_WSTRING, max(size, 1)))
            else:
                fields.append(Field(name, FieldType.V_WSTRING, TEXT))
        return fields

    def columns(self, conn, schema, table):
        return self.describe_query(conn, f'SELECT * FROM {self.qualified(schema, table)}', ())

    def convert(self, f: Field):
        if f.type is FieldType.DATETIME:
            return lambda v: None if v is None else (v.astimezone(_dt.timezone.utc).replace(tzinfo=None)
                                                     if isinstance(v, _dt.datetime) and v.tzinfo else v)
        if f.type is FieldType.V_WSTRING:
            return lambda v: v if v is None or isinstance(v, str) else str(v)
        if f.type is FieldType.DOUBLE:
            return lambda v: None if v is None else float(v)
        if f.type is FieldType.BOOL:
            return lambda v: None if v is None else bool(v)
        return None

    def limit(self, sql, n):
        return f'SELECT TOP ({int(n)}) * FROM ({sql}) flowbench_limited'


def _ms_message(e) -> str:
    args = getattr(e, 'args', ())
    if args and isinstance(args[0], tuple) and len(args[0]) > 1:
        msg = args[0][1]
        return (msg.decode('utf-8', 'replace') if isinstance(msg, bytes) else str(msg)).split('DB-Lib error')[0].strip()
    return str(e)


def dialect_for(conn_cfg, password) -> Dialect:
    if conn_cfg.dialect == 'mongodb':
        from ..errors import ConfigError
        raise ConfigError(f'{conn_cfg.name!r} is a MongoDB connection; use the MongoDB Input and Output tools')
    return {'postgresql': PostgreSQL, 'sqlserver': SQLServer}[conn_cfg.dialect](conn_cfg, password)


# =========================================================================== query execution

class Query:
    """One read query with timeout, cancellation and batched, typed fetching."""

    def __init__(self, dialect: Dialect, sql: str, params=(), *, cancel_token=None, read_only=True):
        self.d, self.sql, self.params = dialect, sql, tuple(params or ())
        self.cancel_token = cancel_token
        self.read_only = read_only
        self.rows_fetched = 0
        self.started = None
        self.first_row_ms = None

    def __enter__(self):
        if self.read_only:
            check_read_only_sql(self.sql)
        self.conn = self.d.connect(read_only=self.read_only)
        if self.read_only and self.d.name == 'sqlserver':
            # SQL Server has no session-level READ ONLY transaction mode. Keep the
            # driver connection in an explicit transaction and unconditionally roll
            # it back in __exit__; database-user permissions remain the primary guard.
            try:
                self.conn.cursor().execute('BEGIN TRANSACTION')
            except Exception as e:
                try:
                    self.conn.close()
                finally:
                    raise self._wrap(e) from None
        self._stop = threading.Event()
        self._cancelled = False
        if self.cancel_token is not None:
            def watch():
                while not self._stop.wait(0.1):
                    if self.cancel_token.cancelled:
                        self._cancelled = True
                        self.d.cancel(self.conn)
                        return
            threading.Thread(target=watch, daemon=True).start()
        return self

    def describe(self) -> list[Field]:
        try:
            return self.d.describe_query(self.conn, self.sql, self.params)
        except Exception as e:
            raise self._wrap(e) from None

    def batches(self, fields: list[Field], batch_rows: int, max_rows: int | None = None):
        """Yield lists of converted rows (tuples)."""
        self.started = time.perf_counter()
        try:
            cur = self.d.run(self.conn, self.sql, self.params)
            convs = [self.d.convert(f) for f in fields]
            while True:
                want = batch_rows if max_rows is None else min(batch_rows, max_rows - self.rows_fetched)
                if want <= 0:
                    break
                rows = cur.fetchmany(want)
                if not rows:
                    break
                if self.first_row_ms is None:
                    self.first_row_ms = (time.perf_counter() - self.started) * 1000
                if any(convs):
                    rows = [tuple(c(v) if c else v for c, v in zip(convs, r)) for r in rows]
                self.rows_fetched += len(rows)
                yield rows
                if self._cancelled:
                    break
        except Exception as e:
            raise self._wrap(e) from None
        if self._cancelled or (self.cancel_token is not None and self.cancel_token.cancelled):
            raise CancelledError('Database query cancelled')

    def _wrap(self, e):
        if isinstance(e, EngineError):
            return e
        if self._cancelled:
            return CancelledError('Database query cancelled')
        text = self.d._scrub(_ms_message(e) if self.d.name == 'sqlserver' else str(e)).strip()
        if re.search(r'read[- ]only|cannot execute .* in a read-only transaction', text, re.I):
            return ReadOnlyViolation(text)
        if re.search(r'statement timeout|canceling statement due to statement timeout|timeout expired|query timeout',
                     text, re.I):
            return DatabaseError(f'Query exceeded the time limit: {text}', code='database-timeout')
        return DatabaseError(text)

    def __exit__(self, *exc):
        self._stop.set()
        try:
            self.conn.rollback()  # reads never commit anything
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
        return False
