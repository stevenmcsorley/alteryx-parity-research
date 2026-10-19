"""ODBC Loader: read the metadata (schemas, tables, columns, procedures) of any ODBC data source; no vendor code.

Functional source: the public ODBC Loader help ("reads basic metadata - schemas, tables, columns, and procedures -
from the ODBC layer"). A saved database connection of type ODBC names the driver (or a DSN); the password lives
in the OS vault. Uses pyodbc (MIT) over the system's ODBC driver manager (built in on Windows, unixODBC on Linux and
macOS); the driver itself comes from the database vendor or an open-source project.
"""
from __future__ import annotations

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema

TEXT = 2**30 - 1


def _t(name):
    return Field(name, FieldType.V_WSTRING, TEXT)


COLUMNS = Schema([_t('Catalog'), _t('Schema'), _t('Table'), _t('TableType'), _t('Column'), Field('Ordinal', FieldType.INT32),
                  _t('TypeName'), Field('SqlType', FieldType.INT32), Field('Size', FieldType.INT64),
                  Field('Decimals', FieldType.INT32), Field('Nullable', FieldType.BOOL), _t('Remarks')])
PROCEDURES = Schema([_t('Catalog'), _t('Schema'), _t('Procedure'), Field('ProcedureType', FieldType.INT32), _t('Remarks')])


def connection_string(cfg, password) -> str:
    def braced(v):
        # Brace a value only when ODBC needs it: several drivers (SQLite's among them) keep the braces otherwise.
        v = str(v)
        return '{' + v + '}' if any(c in v for c in ';={}') or v != v.strip() else v
    parts = [f"DSN={braced(cfg.options['dsn'])}"] if cfg.options.get('dsn') else [f"Driver={braced(cfg.options['driver'])}"]
    if cfg.host:
        parts.append(f'Server={braced(cfg.host)}')
    if cfg.port:
        parts.append(f'Port={cfg.port}')
    if cfg.database:
        parts.append(f'Database={braced(cfg.database)}')
    if cfg.user:
        parts.append(f'UID={braced(cfg.user)}')
    if password:
        if '}' in password:
            raise ConfigError('The ODBC password may not contain }')
        parts.append(f'PWD={braced(password)}')
    return ';'.join(parts)


class OdbcLoader(Tool):
    manifest = ToolManifest(
        tool_id='core.odbc_loader', version='1.0.0', display_name='ODBC Loader', catalog_ids=('connect:odbc-loader',),
        inputs=(), outputs=(Anchor('columns', 'O', description='One record per column of each table or view'),
                            Anchor('procedures', 'P', description='One record per stored procedure')),
        side_effects='network', deterministic=False,
        description='Read the metadata of an ODBC data source: every table and view with its columns (type, size, '
                    'decimals, nullability, remarks), and the stored procedures. Optional schema, table and table-type filters.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['provider', 'connection_id'], 'properties': {
            'provider': {'type': 'string', 'enum': ['odbc']}, 'provider_semantics': {'type': 'string'},
            'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'database:odbc',
                              'description': 'Saved ODBC connection (Databases in the toolbar, type ODBC)'},
            'schema': {'type': ['string', 'null'], 'default': None, 'description': 'Schema name or ODBC pattern (%)'},
            'table': {'type': ['string', 'null'], 'default': None, 'description': 'Table name or ODBC pattern (%)'},
            'table_types': {'type': 'array', 'items': {'type': 'string'}, 'default': ['TABLE', 'VIEW']},
            'include_procedures': {'type': 'boolean', 'default': True},
            'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 600, 'default': 30}}})

    def effects(self, cfg):
        return {'network_requests'}

    def infer(self, ctx, cfg, inputs):
        return {'columns': COLUMNS, 'procedures': PROCEDURES}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        from ..db.connections import ConnectionStore
        providers.resolve(cfg, 'odbc')
        try:
            import pyodbc
        except ImportError:
            raise ConfigError('The ODBC Loader needs pyodbc and an ODBC driver manager; see Settings > Integrations') from None
        store = ConnectionStore()
        conn_cfg = store.get(cfg['connection_id'])
        if conn_cfg.dialect != 'odbc':
            raise ConfigError(f'{conn_cfg.name!r} is not an ODBC connection')
        try:
            password = store.secret(cfg['connection_id'])
        except Exception:
            password = None  # drivers such as SQLite need none
        try:
            conn = pyodbc.connect(connection_string(conn_cfg, password), timeout=cfg['timeout_seconds'], autocommit=True,
                                  readonly=True)
        except pyodbc.Error as e:
            raise DataError(f'Could not connect through ODBC: {e.args[-1] if e.args else e}'.replace(password or '\0', '***')) from None
        columns, procedures = [], []
        try:
            cur = conn.cursor()
            types = ','.join(cfg['table_types']) or None
            tables = [tuple(r)[:4] for r in cur.tables(table=cfg['table'], schema=cfg['schema'], tableType=types)]
            for catalog, schema, table, kind in tables:
                ctx.check_cancelled()
                for c in cur.columns(table=table, schema=schema, catalog=catalog):
                    columns.append((catalog, schema, table, kind, c.column_name, c.ordinal_position, c.type_name, c.data_type,
                                    c.column_size, c.decimal_digits, None if c.nullable == 2 else bool(c.nullable), c.remarks))
            if cfg['include_procedures']:
                try:
                    for p in cur.procedures(schema=cfg['schema']):
                        procedures.append((p.procedure_cat, p.procedure_schem, p.procedure_name, p.procedure_type, p.remarks))
                except pyodbc.Error:
                    ctx.warn('This ODBC driver does not list stored procedures')
        except pyodbc.Error as e:
            raise DataError(f'Reading ODBC metadata failed: {e.args[-1] if e.args else e}') from None
        finally:
            conn.close()
        ctx.info(f'{len(tables)} table(s), {len(columns)} column(s), {len(procedures)} procedure(s)')
        return {'columns': Table.from_rows(COLUMNS, columns), 'procedures': Table.from_rows(PROCEDURES, procedures)}


TOOLS = [OdbcLoader]
