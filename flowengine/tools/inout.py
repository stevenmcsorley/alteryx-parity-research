"""In/Out category kernels: Input Data, Output Data, Text Input, Browse, Date Time Now, Directory."""
from __future__ import annotations

import csv
import datetime as _dt
import fnmatch
import io
import json
import os
import tempfile
from itertools import chain as _chain
from pathlib import Path

from ..errors import ConfigError, DataError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value, infer_type
from ._common import FIELD_SPEC, ON_ERROR, cast_column, field_from_spec

UNBOUNDED_TEXT = 2**30 - 1  # declared size for untyped text fields: never truncates

_FORMATS = ['auto', 'csv', 'tsv', 'delimited', 'json', 'jsonl', 'parquet', 'arrow']
_BINARY = ('parquet', 'arrow')


def _resolve_format(fmt: str, path: Path) -> str:
    if fmt != 'auto':
        return fmt
    ext = path.suffix.lower()
    return {'.csv': 'csv', '.tsv': 'tsv', '.tab': 'tsv', '.txt': 'delimited', '.json': 'json',
            '.jsonl': 'jsonl', '.ndjson': 'jsonl', '.parquet': 'parquet', '.arrow': 'arrow',
            '.feather': 'arrow', '.ipc': 'arrow'}.get(ext) or _unsupported(ext)


def _unsupported(ext):
    raise ConfigError(f'Cannot infer file format from extension {ext!r}; set "format" explicitly')


def _delimiter(fmt, cfg):
    return '\t' if fmt == 'tsv' else (cfg['delimiter'] if fmt == 'delimited' else ',')


# =========================================================================== Input Data

class InputData(Tool):
    manifest = ToolManifest(
        tool_id='core.input_data', version='1.0.0', display_name='Input Data',
        catalog_ids=('in-out:input-data',),
        inputs=(),
        outputs=(Anchor('output', 'O', description='Records read from the file'),),
        side_effects='reads-files', deterministic=True,
        description='Read delimited text (CSV/TSV/custom), JSON/JSON Lines, Parquet or Arrow IPC. Database and '
                    'other formats are separate adapters, not yet implemented.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['path'],
            'properties': {
                'path': {'type': 'string', 'minLength': 1},
                'format': {'enum': _FORMATS, 'default': 'auto'},
                'delimiter': {'type': 'string', 'minLength': 1, 'maxLength': 1, 'default': ','},
                'quote_char': {'type': 'string', 'minLength': 1, 'maxLength': 1, 'default': '"'},
                'has_header': {'type': 'boolean', 'default': True},
                'encoding': {'type': 'string', 'default': 'utf-8-sig'},
                'skip_rows': {'type': 'integer', 'minimum': 0, 'default': 0},
                'max_records': {'type': ['integer', 'null'], 'minimum': 0, 'default': None},
                'fields': {'type': 'array', 'items': FIELD_SPEC,
                           'description': 'Optional declared schema; values are converted to these types.'},
                'on_conversion_error': ON_ERROR,
                'ragged_rows': {'enum': ['error', 'pad'], 'default': 'error'},
                'empty_as_null': {'type': 'boolean', 'default': False,
                                  'description': 'Treat empty delimited values as null instead of empty string'},
                'add_file_name_field': {'type': ['string', 'null'], 'default': None},
            },
        })

    def check_config(self, cfg):
        for spec in cfg.get('fields', []):
            field_from_spec(spec)

    def _delimited_rows(self, ctx, cfg, path, fmt, header_only=False):
        """Generator: yields the header name list first, then normalised body rows (lists of str/None).

        Shared by batch reads and streaming reads so both parse identically."""
        delim = _delimiter(fmt, cfg)
        try:
            fh = open(path, 'r', encoding=cfg['encoding'], newline='')
        except FileNotFoundError:
            raise DataError(f'Input file not found: {path}') from None
        except LookupError:
            raise ConfigError(f"Unknown encoding {cfg['encoding']!r}") from None
        with fh:
            reader = csv.reader(fh, delimiter=delim, quotechar=cfg['quote_char'], strict=True)
            try:
                for _ in range(cfg['skip_rows']):
                    next(reader, None)
                first = next(reader, None)
                if first is None:
                    if cfg['has_header']:
                        raise DataError(f'{path.name}: file is empty; a header row was expected')
                    yield []
                    return
                if cfg['has_header']:
                    names = [n.strip() for n in first]
                    pending = None
                else:
                    names = [f'Field_{i + 1}' for i in range(len(first))]
                    pending = first
                if '' in names:
                    raise DataError(f'{path.name}: header contains an empty field name')
                if len(set(names)) != len(names):
                    raise DataError(f'{path.name}: duplicate header names '
                                    f'{sorted({n for n in names if names.count(n) > 1})}')
                yield names
                if header_only:
                    return
                limit, width, count, ragged = cfg['max_records'], len(names), 0, 0
                body = reader if pending is None else _chain([pending], reader)
                for row in body:
                    if limit is not None and count >= limit:
                        break
                    if not row:  # blank line
                        continue
                    if len(row) != width:
                        if cfg['ragged_rows'] == 'error':
                            raise DataError(f'{path.name}: record {count + 1} has {len(row)} values; expected {width}')
                        row = (row + [None] * width)[:width]
                        ragged += 1
                    count += 1
                    if count % 50000 == 0:
                        ctx.check_cancelled()
                    yield row
                if ragged:
                    ctx.warn(f'{ragged} record(s) had a different number of values and were padded/truncated',
                             count=ragged)
            except csv.Error as e:
                raise DataError(f'{path.name}: malformed delimited data at line {reader.line_num}: {e}') from None
            except UnicodeDecodeError as e:
                raise DataError(f"{path.name}: cannot decode as {cfg['encoding']}: {e.reason}") from None

    def _read_delimited(self, ctx, cfg, path, fmt, header_only=False):
        gen = self._delimited_rows(ctx, cfg, path, fmt, header_only)
        names = next(gen)
        return names, list(gen)

    # ---------------------------------------------------------------- streaming source
    streaming_source = True

    def can_stream(self, cfg):
        return True

    def iter_batches(self, ctx, cfg, batch_rows):
        """Yield typed batches of at most ``batch_rows`` records. Delimited text and Parquet are read
        incrementally; JSON/Arrow IPC are read whole and then batched."""
        path = ctx.resolve_path(cfg['path'])
        fmt = _resolve_format(cfg['format'], path)
        declared = [field_from_spec(s) for s in cfg['fields']] if cfg.get('fields') else None
        fname = cfg['add_file_name_field']
        if fmt in ('csv', 'tsv', 'delimited'):
            gen = self._delimited_rows(ctx, cfg, path, fmt)
            names = next(gen)
            if declared and [f.name for f in declared] != names:
                raise SchemaError(f'{path.name}: declared fields {[f.name for f in declared]} do not match file '
                                  f'fields {names}')
            fields = declared or [Field(n, FieldType.V_WSTRING, UNBOUNDED_TEXT) for n in names]
            if fname:
                fields = fields + [Field(fname, FieldType.V_WSTRING, UNBOUNDED_TEXT)]
            schema = Schema(fields)
            empty_null = cfg['empty_as_null']
            buf, offset = [], 0
            for row in _chain(gen, [None]):
                if row is not None:
                    buf.append(row)
                if buf and (len(buf) >= batch_rows or row is None):
                    cols = [[(None if (empty_null and r[i] == '') else r[i]) for r in buf] for i in range(len(names))]
                    if declared:
                        cols = [cast_column(c, f, ctx, cfg['on_conversion_error'], row_offset=offset)
                                for c, f in zip(cols, declared)]
                    if fname:
                        cols.append([str(path)] * len(buf))
                    yield Table(schema, cols)
                    offset += len(buf)
                    buf = []
            return
        if fmt == 'parquet' and not declared and not fname:
            from .. import arrow
            arrow.require()
            import pyarrow.parquet as pq
            try:
                pf = pq.ParquetFile(path)
            except FileNotFoundError:
                raise DataError(f'Input file not found: {path}') from None
            remaining = cfg['max_records']
            for rb in pf.iter_batches(batch_size=batch_rows):
                t = arrow.from_arrow(arrow.pa.Table.from_batches([rb]))
                if remaining is not None:
                    if remaining <= 0:
                        return
                    t = t.take(range(min(remaining, len(t))))
                    remaining -= len(t)
                yield t
            return
        schema, cols = self._schema_and_columns(ctx, cfg)
        t = Table(schema, cols) if cols else Table.empty(schema)
        for i in range(0, len(t), batch_rows):
            yield t.take(range(i, min(i + batch_rows, len(t))))

    def _read_json(self, ctx, cfg, path, fmt):
        try:
            text = path.read_text(encoding=cfg['encoding'])
        except FileNotFoundError:
            raise DataError(f'Input file not found: {path}') from None
        try:
            if fmt == 'jsonl':
                records = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                records = json.loads(text)
        except json.JSONDecodeError as e:
            raise DataError(f'{path.name}: invalid JSON at line {e.lineno} column {e.colno}: {e.msg}') from None
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise DataError(f'{path.name}: expected an array of objects (or one object per line)')
        if cfg['max_records'] is not None:
            records = records[:cfg['max_records']]
        names: list[str] = []
        for r in records:
            for k in r:
                if k not in names:
                    names.append(k)
        cols = []
        for n in names:
            col = []
            for r in records:
                v = r.get(n)
                col.append(json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (dict, list)) else v)
            cols.append(col)
        return names, cols

    def _schema_and_columns(self, ctx, cfg, header_only=False):
        path = ctx.resolve_path(cfg['path'])
        fmt = _resolve_format(cfg['format'], path)
        if fmt in _BINARY:
            from .. import arrow
            if not path.is_file():
                raise DataError(f'Input file not found: {path}')
            try:
                t = arrow.read_parquet(path) if fmt == 'parquet' else arrow.read_ipc(path)
            except DataError:
                raise
            except Exception as e:  # pyarrow raises many exception types for corrupt files
                raise DataError(f'{path.name}: cannot read {fmt}: {e}') from None
            if cfg['max_records'] is not None:
                t = t.take(range(min(cfg['max_records'], len(t))))
            names, cols, natural = t.schema.names, [list(c) for c in t.columns], list(t.schema)
        elif fmt in ('json', 'jsonl'):
            names, cols = self._read_json(ctx, cfg, path, fmt)
            natural = []
            for n, col in zip(names, cols):
                kinds = {infer_type(v) for v in col if v is not None}
                if kinds <= {FieldType.INT64}:
                    t = FieldType.INT64 if kinds else FieldType.V_WSTRING
                elif kinds <= {FieldType.INT64, FieldType.DOUBLE}:
                    t = FieldType.DOUBLE
                elif kinds == {FieldType.BOOL}:
                    t = FieldType.BOOL
                else:
                    t = FieldType.V_WSTRING
                    col[:] = [v if v is None or isinstance(v, str) else format_value(v) for v in col]
                natural.append(Field(n, t, UNBOUNDED_TEXT if t is FieldType.V_WSTRING else None))
        else:
            names, rows = self._read_delimited(ctx, cfg, path, fmt, header_only)
            empty_null = cfg['empty_as_null']
            cols = [[(None if (empty_null and r[i] == '') else r[i]) for r in rows] for i in range(len(names))]
            natural = [Field(n, FieldType.V_WSTRING, UNBOUNDED_TEXT) for n in names]
        if cfg.get('fields'):
            declared = [field_from_spec(s) for s in cfg['fields']]
            if [f.name for f in declared] != names:
                raise SchemaError(f'{path.name}: declared fields {[f.name for f in declared]} do not match file '
                                  f'fields {names}')
            cols = [cast_column(c, f, ctx, cfg['on_conversion_error']) for c, f in zip(cols, declared)]
            fields = declared
        else:
            fields = natural
        if cfg['add_file_name_field']:
            fields = fields + [Field(cfg['add_file_name_field'], FieldType.V_WSTRING, UNBOUNDED_TEXT)]
            cols = cols + [[str(path)] * (len(cols[0]) if cols else 0)]
        return Schema(fields), cols

    def infer(self, ctx, cfg, inputs):
        path = ctx.resolve_path(cfg['path'])
        if not path.is_file():
            return None  # reported at run time
        if _resolve_format(cfg['format'], path) in ('json', 'jsonl') + _BINARY:
            return {'output': self._schema_and_columns(ctx, cfg)[0]}
        return {'output': self._schema_and_columns(ctx, cfg, header_only=True)[0]}

    def run(self, ctx, cfg, inputs):
        schema, cols = self._schema_and_columns(ctx, cfg)
        table = Table(schema, cols) if cols else Table.empty(schema)
        ctx.info(f'read {len(table)} records', path=str(ctx.resolve_path(cfg['path'])))
        return {'output': table}


# =========================================================================== Output Data

class OutputData(Tool):
    manifest = ToolManifest(
        tool_id='core.output_data', version='1.0.0', display_name='Output Data',
        catalog_ids=('in-out:output-data',),
        inputs=(Anchor('input', 'I', description='Records to write'),),
        outputs=(),
        side_effects='writes-files', deterministic=True,
        description='Write records atomically to delimited text, JSON/JSON Lines, Parquet or Arrow IPC.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['path'],
            'properties': {
                'path': {'type': 'string', 'minLength': 1},
                'format': {'enum': _FORMATS, 'default': 'auto'},
                'delimiter': {'type': 'string', 'minLength': 1, 'maxLength': 1, 'default': ','},
                'write_header': {'type': 'boolean', 'default': True},
                'encoding': {'type': 'string', 'default': 'utf-8'},
                'if_exists': {'enum': ['overwrite', 'error'], 'default': 'overwrite'},
                'null_text': {'type': 'string', 'default': ''},
                'line_terminator': {'enum': ['\n', '\r\n'], 'default': '\n'},
                'quoting': {'enum': ['minimal', 'all'], 'default': 'minimal'},
            },
        })

    def infer(self, ctx, cfg, inputs):
        return {}

    def can_stream(self, cfg):
        return True

    def open_sink(self, ctx, cfg, schema):
        return _FileSink(ctx, cfg, schema)

    def run(self, ctx, cfg, inputs):
        table: Table = inputs['input']
        sink = self.open_sink(ctx, cfg, table.schema)
        try:
            sink.write(table)
            sink.close()
        except BaseException:
            sink.abort()
            raise
        return {}


class _FileSink:
    """Atomic, incremental writer for every Output Data format (temp file, then rename)."""

    def __init__(self, ctx, cfg, schema):
        self.ctx, self.cfg, self.schema = ctx, cfg, schema
        self.path = ctx.resolve_path(cfg['path'])
        self.fmt = _resolve_format(cfg['format'], self.path)
        if self.path.exists() and cfg['if_exists'] == 'error':
            raise DataError(f'Output file already exists: {self.path}')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, self.tmp = tempfile.mkstemp(dir=self.path.parent, prefix='.flowengine-', suffix=self.path.suffix)
        self.rows = 0
        self.dt = ctx.profile.double_text if ctx.profile else 'g15'
        self._writer = None
        self._sink = None
        if self.fmt in _BINARY:
            os.close(fd)
            from .. import arrow
            arrow.require()
            self.fh = None
            empty = arrow.to_arrow(Table.empty(schema))
            if self.fmt == 'parquet':
                import pyarrow.parquet as pq
                self._writer = pq.ParquetWriter(self.tmp, empty.schema)
            else:
                self._sink = arrow.pa.OSFile(self.tmp, 'wb')  # closed explicitly: the IPC writer does not
                self._writer = arrow.pa.ipc.new_file(self._sink, empty.schema)
            return
        self.fh = os.fdopen(fd, 'w', encoding=cfg['encoding'], newline='')
        if self.fmt == 'json':
            self.fh.write('[')
        elif self.fmt not in ('jsonl',):
            self._csv = csv.writer(self.fh, delimiter=_delimiter(self.fmt, cfg), lineterminator=cfg['line_terminator'],
                                   quoting=csv.QUOTE_ALL if cfg['quoting'] == 'all' else csv.QUOTE_MINIMAL)
            if cfg['write_header']:
                self._csv.writerow(schema.names)

    def write(self, table: Table):
        if tuple(table.schema) != tuple(self.schema):
            raise DataError('Output Data received batches with differing schemas')
        try:
            if self._writer is not None:
                from .. import arrow
                if len(table):
                    at = arrow.to_arrow(table)
                    if self.fmt == 'parquet':
                        self._writer.write_table(at)
                    else:
                        self._writer.write_table(at)
            elif self.fmt in ('json', 'jsonl'):
                names = table.schema.names
                for r in table.rows():
                    rec = json.dumps({k: _json_value(v, self.dt) for k, v in zip(names, r)}, ensure_ascii=False)
                    if self.fmt == 'json':
                        self.fh.write(('\n  ' if self.rows == 0 else ',\n  ') + rec)
                    else:
                        self.fh.write(rec + '\n')
                    self.rows += 1
            else:
                null = self.cfg['null_text']
                for r in table.rows():
                    self._csv.writerow([null if v is None else format_value(v, self.dt) for v in r])
                    self.rows += 1
                    if self.rows % 50000 == 0:
                        self.ctx.check_cancelled()
        except UnicodeEncodeError as e:
            raise DataError(f"Value cannot be encoded as {self.cfg['encoding']}: {e.reason}") from None
        if self._writer is not None:
            self.rows += len(table)

    def close(self):
        if self._writer is not None:
            self._writer.close()
            if self._sink is not None:
                self._sink.close()
        else:
            if self.fmt == 'json':
                self.fh.write('\n]\n' if self.rows else ']\n')
            self.fh.close()
        os.replace(self.tmp, self.path)
        self.ctx.info(f'wrote {self.rows} records', path=str(self.path))

    def abort(self):
        try:
            if self._writer is not None:
                self._writer.close()
                if self._sink is not None:
                    self._sink.close()
            elif self.fh and not self.fh.closed:
                self.fh.close()
        finally:
            if os.path.exists(self.tmp):
                os.unlink(self.tmp)


def _json_value(v, double_text='g15'):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v
    return format_value(v, double_text)


# =========================================================================== Text Input

class TextInput(Tool):
    manifest = ToolManifest(
        tool_id='core.text_input', version='1.0.0', display_name='Text Input',
        catalog_ids=('in-out:text-input',),
        inputs=(),
        outputs=(Anchor('output', 'O'),),
        description='Records typed directly into the workflow configuration.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['fields', 'rows'],
            'properties': {
                'fields': {'type': 'array', 'minItems': 1, 'items': FIELD_SPEC},
                'rows': {'type': 'array', 'items': {'type': 'array',
                                                    'items': {'type': ['string', 'number', 'boolean', 'null']}}},
            },
        })

    def check_config(self, cfg):
        fields = [field_from_spec(s) for s in cfg['fields']]
        Schema(fields)
        for i, r in enumerate(cfg['rows']):
            if len(r) != len(fields):
                raise ConfigError(f'Row {i + 1} has {len(r)} values; {len(fields)} fields are defined')

    def run(self, ctx, cfg, inputs):
        fields = [field_from_spec(s) for s in cfg['fields']]
        cols = [cast_column([r[i] for r in cfg['rows']], f, ctx, 'error') for i, f in enumerate(fields)]
        return {'output': Table(fields, cols)}


# =========================================================================== Browse

class Browse(Tool):
    manifest = ToolManifest(
        tool_id='core.browse', version='1.0.0', display_name='Browse',
        catalog_ids=('in-out:browse',),
        inputs=(Anchor('input', 'I'),), outputs=(),
        description='Capture the complete incoming record set into the run result for inspection.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {}})

    def infer(self, ctx, cfg, inputs):
        return {}

    def run(self, ctx, cfg, inputs):
        if ctx.browse_sink and not ctx.dry_run:
            ctx.browse_sink(ctx.node_id, inputs['input'])
        return {}


# =========================================================================== Date Time Now

class DateTimeNow(Tool):
    manifest = ToolManifest(
        tool_id='core.date_time_now', version='1.0.0', display_name='Date Time Now',
        catalog_ids=('in-out:date-time-now',),
        inputs=(), outputs=(Anchor('output', 'O'),), deterministic=False,
        description="One record holding the run clock's current date/time. Inject a fixed clock for reproducible runs.",
        config_schema={
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'field_name': {'type': 'string', 'minLength': 1, 'default': 'DateTime_Out'},
                'output': {'enum': ['datetime', 'date', 'time', 'text'], 'default': 'datetime'},
                'text_format': {'type': 'string', 'default': '%Y-%m-%d %H:%M:%S'},
            },
        })

    def run(self, ctx, cfg, inputs):
        kind = cfg['output']
        ftype = {'datetime': FieldType.DATETIME, 'date': FieldType.DATE, 'time': FieldType.TIME,
                 'text': FieldType.V_WSTRING}[kind]
        field = Field(cfg['field_name'], ftype)
        if ctx.dry_run:
            return {'output': Table.empty([field])}
        now = ctx.clock().replace(microsecond=0)
        value = {'datetime': now, 'date': now.date(), 'time': now.time(),
                 'text': None}[kind] if kind != 'text' else now.strftime(cfg['text_format'])
        return {'output': Table([field], [[value]])}


# =========================================================================== Directory

class Directory(Tool):
    manifest = ToolManifest(
        tool_id='core.directory', version='1.0.0', display_name='Directory',
        catalog_ids=('in-out:directory',),
        inputs=(), outputs=(Anchor('output', 'O'),),
        side_effects='reads-files', deterministic=False,
        description='List files in a directory (metadata only), sorted by full path.',
        config_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['path'],
            'properties': {
                'path': {'type': 'string', 'minLength': 1},
                'pattern': {'type': 'string', 'default': '*'},
                'recursive': {'type': 'boolean', 'default': False},
                'include_hidden': {'type': 'boolean', 'default': False},
            },
        })

    SCHEMA = Schema([Field('FullPath', FieldType.V_WSTRING, UNBOUNDED_TEXT),
                     Field('Directory', FieldType.V_WSTRING, UNBOUNDED_TEXT),
                     Field('FileName', FieldType.V_WSTRING, UNBOUNDED_TEXT),
                     Field('Extension', FieldType.V_WSTRING, UNBOUNDED_TEXT),
                     Field('SizeBytes', FieldType.INT64),
                     Field('LastModifiedUTC', FieldType.DATETIME)])

    def infer(self, ctx, cfg, inputs):
        return {'output': self.SCHEMA}

    def run(self, ctx, cfg, inputs):
        root = ctx.resolve_path(cfg['path'])
        if not root.is_dir():
            raise DataError(f'Directory not found: {root}')
        it = root.rglob('*') if cfg['recursive'] else root.glob('*')
        rows = []
        for p in it:
            rel_parts = p.relative_to(root).parts
            if not cfg['include_hidden'] and any(part.startswith('.') for part in rel_parts):
                continue
            if not p.is_file() or not fnmatch.fnmatch(p.name, cfg['pattern']):
                continue
            st = p.stat()
            rows.append((str(p), str(p.parent), p.name, p.suffix, st.st_size,
                         _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc).replace(tzinfo=None, microsecond=0)))
        rows.sort(key=lambda r: r[0])
        return {'output': Table.from_rows(self.SCHEMA, rows)}


TOOLS = [InputData, OutputData, TextInput, Browse, DateTimeNow, Directory]
