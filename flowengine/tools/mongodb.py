"""MongoDB Input and Output with pymongo (Apache-2.0); no vendor code.

Functional source: the public MongoDB Input/Output tool help. A saved database connection
(Databases in the toolbar, type MongoDB) holds host, port, database, user and TLS settings; the
password lives in the OS vault. ``auth_source`` (connection option, default ``admin``) names the
database that holds the user.

Input runs a find (filter, projection, sort, limit as MongoDB Extended JSON) and returns either one
field per top-level key (nested documents and arrays as JSON text) or one JSON text field per
document. Output inserts documents, or upserts them on key fields; it needs a connection that
allows writes and the run's database-write permission. Replacing a collection deletes its
documents first and needs ``confirm_replace``.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from decimal import Decimal

from ..errors import ConfigError, DataError, EngineError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, cast_value, type_for_values
from ._common import require_fields

TEXT = 2**30 - 1
_COLLECTION = re.compile(r'^(?!system\.)[^$\0]{1,120}$')


def _client(conn_id, write=False):
    import pymongo
    from ..db.connections import ConnectionStore
    store = ConnectionStore()
    cfg = store.get(conn_id)
    if cfg.dialect != 'mongodb':
        raise ConfigError(f'{cfg.name!r} is not a MongoDB connection')
    if write and not cfg.allow_writes:
        raise EngineError(f'Connection {cfg.name!r} is read-only; enable writes on the connection first',
                          code='permission-denied')
    tls = cfg.tls in ('require', 'verify-full')
    kwargs = dict(host=cfg.host, port=int(cfg.port), username=cfg.user, password=store.secret(conn_id),
                  authSource=cfg.options.get('auth_source', 'admin'), tls=tls,
                  connectTimeoutMS=cfg.connect_timeout * 1000, serverSelectionTimeoutMS=cfg.connect_timeout * 1000,
                  socketTimeoutMS=(cfg.query_timeout * 1000) or None, appname='Flowbench', tz_aware=True)
    if tls and cfg.ca_file:
        kwargs['tlsCAFile'] = cfg.ca_file
    if cfg.tls == 'require':
        kwargs['tlsAllowInvalidHostnames'] = True  # encrypted, certificate chain checked; host name not matched
    return pymongo.MongoClient(**kwargs), cfg


def _ejson(text, what):
    from bson import json_util
    if text in (None, ''):
        return None
    try:
        return json_util.loads(text)
    except (ValueError, TypeError) as e:
        raise ConfigError(f'{what} is not valid MongoDB Extended JSON: {e}') from None


def _plain(v):
    """A BSON value as a Flowbench cell: scalars keep their type, everything else is canonical JSON text."""
    from bson import Decimal128, ObjectId, json_util
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, ObjectId):
        return str(v)
    if isinstance(v, Decimal128):
        return v.to_decimal()
    if isinstance(v, _dt.datetime):
        return v.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return json_util.dumps(v, json_options=json_util.RELAXED_JSON_OPTIONS)


def _cell(v, f: Field):
    if v is None:
        return None
    if f.type is FieldType.DATETIME:
        return v.isoformat(sep=' ')
    if isinstance(v, Decimal):
        return str(v)
    return v


class MongoDBInput(Tool):
    manifest = ToolManifest(
        tool_id='core.mongodb_input', version='1.0.0', display_name='MongoDB Input', catalog_ids=('connectors:mongodb-input',),
        inputs=(), outputs=(Anchor('output', 'O', description='Documents read from the collection'),),
        side_effects='network', deterministic=False,
        description='Read documents from a MongoDB collection with an optional filter, projection, sort and limit '
                    '(MongoDB Extended JSON). Output one field per top-level key (nested values as JSON text) or one '
                    'JSON field per document.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'collection'], 'properties': {
            'provider': {'type': 'string', 'enum': ['mongodb']},
            'provider_semantics': {'type': 'string'},
            'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'database:mongodb',
                              'description': 'Saved MongoDB connection'},
            'collection': {'type': 'string', 'minLength': 1},
            'filter': {'type': ['string', 'null'], 'default': None, 'description': 'For example {"region": "North"}'},
            'projection': {'type': ['string', 'null'], 'default': None, 'description': 'For example {"_id": 0, "name": 1}'},
            'sort': {'type': ['string', 'null'], 'default': None, 'description': 'For example {"amount": -1}'},
            'limit': {'type': 'integer', 'minimum': 0, 'default': 0, 'description': '0 reads every matching document'},
            'output': {'enum': ['fields', 'json'], 'default': 'fields'},
            'json_field': {'type': 'string', 'minLength': 1, 'default': 'JSON'}}})

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'document-database')
        if not _COLLECTION.match(cfg['collection']):
            raise ConfigError(f"{cfg['collection']!r} is not a collection Flowbench reads")
        query = _ejson(cfg['filter'], 'filter') or {}
        projection = _ejson(cfg['projection'], 'projection')
        sort = _ejson(cfg['sort'], 'sort')
        if not isinstance(query, dict) or (projection is not None and not isinstance(projection, dict)) \
                or (sort is not None and not isinstance(sort, dict)):
            raise ConfigError('filter, projection and sort are JSON objects')
        client, conn = _client(cfg['connection_id'])
        import pymongo.errors
        try:
            cursor = client[conn.database][cfg['collection']].find(query, projection, limit=cfg['limit'])
            if sort:
                cursor = cursor.sort(list(sort.items()))
            docs = []
            for doc in cursor:
                ctx.check_cancelled()
                docs.append(doc)
        except pymongo.errors.PyMongoError as e:
            raise DataError(f'MongoDB read failed: {e}') from None
        finally:
            client.close()
        if cfg['output'] == 'json':
            from bson import json_util
            schema = Schema([Field(cfg['json_field'], FieldType.V_WSTRING, TEXT)])
            return {'output': Table.from_rows(schema, [(json_util.dumps(d, json_options=json_util.RELAXED_JSON_OPTIONS),)
                                                       for d in docs])}
        names = []
        for d in docs:
            names.extend(k for k in d if k not in names)
        cols = {n: [_plain(d.get(n)) for d in docs] for n in names}
        fields = []
        for n in names:
            t = type_for_values(cols[n])
            fields.append(Field(n, t, TEXT if t.is_string else (38 if t is FieldType.FIXED_DECIMAL else None),
                                max((len(str(v).split('.')[-1]) for v in cols[n] if isinstance(v, Decimal) and '.' in str(v)), default=0)
                                if t is FieldType.FIXED_DECIMAL else None))
        columns = [[cast_value(_cell(v, f), f)[0] for v in cols[f.name]] for f in fields]
        ctx.info(f"read {len(docs)} documents from {conn.database}.{cfg['collection']}")
        return {'output': Table(Schema(fields), columns) if fields else Table([], [])}


class MongoDBOutput(Tool):
    manifest = ToolManifest(
        tool_id='core.mongodb_output', version='1.0.0', display_name='MongoDB Output', catalog_ids=('connectors:mongodb-output',),
        inputs=(Anchor('input', 'I', description='Records to write as documents'),), outputs=(),
        side_effects='network', deterministic=False,
        description='Write records to a MongoDB collection as documents: insert, upsert on key fields, or replace the '
                    'collection (delete then insert; needs confirm_replace). json_field writes a field that holds whole '
                    'documents as JSON. Needs a writable connection and the database-write permission.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['provider', 'connection_id', 'collection'], 'properties': {
            'provider': {'type': 'string', 'enum': ['mongodb']},
            'provider_semantics': {'type': 'string'},
            'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'database:mongodb',
                              'description': 'Saved MongoDB connection'},
            'collection': {'type': 'string', 'minLength': 1},
            'mode': {'enum': ['insert', 'upsert', 'replace'], 'default': 'insert'},
            'key_fields': {'type': 'array', 'items': {'type': 'string'}, 'uniqueItems': True, 'default': []},
            'confirm_replace': {'type': 'boolean', 'default': False},
            'json_field': {'type': ['string', 'null'], 'default': None,
                           'description': 'Write this field (MongoDB Extended JSON) as the document instead of the record'},
            'batch_size': {'type': 'integer', 'minimum': 1, 'maximum': 100000, 'default': 1000}}})

    def effects(self, cfg):
        return {'database_writes'}

    def infer(self, ctx, cfg, inputs):
        if cfg['mode'] == 'upsert' and not cfg['key_fields']:
            raise ConfigError('Upsert needs key_fields')
        if cfg['json_field']:
            require_fields(inputs['input'], [cfg['json_field']])
        elif cfg['key_fields']:
            require_fields(inputs['input'], cfg['key_fields'])
        return {}

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'document-database')
        self.infer(ctx, cfg, {'input': inputs['input'].schema})
        if not _COLLECTION.match(cfg['collection']):
            raise ConfigError(f"{cfg['collection']!r} is not a collection Flowbench writes")
        if cfg['mode'] == 'replace' and not cfg['confirm_replace']:
            raise ConfigError('Replace deletes every document in the collection first; set confirm_replace to run it')
        if not ctx.limits.get('allow_database_writes'):
            raise EngineError('Database writes are disabled for this run; grant them explicitly '
                              '(CLI --allow-database-writes or confirm in the desktop app)', code='permission-denied')
        table: Table = inputs['input']
        docs = []
        for rec in table.to_dicts():
            if cfg['json_field']:
                doc = _ejson(rec[cfg['json_field']], f"{cfg['json_field']} value")
                if not isinstance(doc, dict):
                    raise DataError(f"{cfg['json_field']} must hold a JSON object for each record")
            else:
                doc = {k: (float(v) if isinstance(v, Decimal) else
                           _dt.datetime.combine(v, _dt.time()) if isinstance(v, _dt.date) and not isinstance(v, _dt.datetime) else
                           v.isoformat() if isinstance(v, _dt.time) else v) for k, v in rec.items()}
            docs.append(doc)
        client, conn = _client(cfg['connection_id'], write=True)
        import pymongo
        import pymongo.errors
        written = 0
        try:
            coll = client[conn.database][cfg['collection']]
            if cfg['mode'] == 'replace':
                coll.delete_many({})
            for start in range(0, len(docs), cfg['batch_size']):
                ctx.check_cancelled()
                batch = docs[start:start + cfg['batch_size']]
                if cfg['mode'] == 'upsert':
                    ops = [pymongo.ReplaceOne({k: d.get(k) for k in cfg['key_fields']}, d, upsert=True) for d in batch]
                    coll.bulk_write(ops, ordered=True)
                else:
                    coll.insert_many(batch, ordered=True)
                written += len(batch)
        except pymongo.errors.PyMongoError as e:
            raise DataError(f'MongoDB write failed after {written} documents: {e}') from None
        finally:
            client.close()
        ctx.info(f"wrote {written} documents to {conn.database}.{cfg['collection']} ({cfg['mode']})")
        return {}


TOOLS = [MongoDBInput, MongoDBOutput]
