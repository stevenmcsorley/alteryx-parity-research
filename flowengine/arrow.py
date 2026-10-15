"""Apache Arrow interoperability (pyarrow).

Lossless conversion between :class:`Table` and ``pyarrow.Table``. Field metadata
(our type name, size, scale) is stored in Arrow field metadata so a round trip
restores the exact flowengine schema; foreign Arrow data is mapped to the
closest flowengine type. Used by Parquet / Arrow IPC file adapters and by the
spill manager.
"""
from __future__ import annotations

from decimal import Decimal

from .errors import DataError, SchemaError
from .table import Table
from .types import Field, FieldType, Schema

try:  # optional at import time so the core engine works without pyarrow
    import pyarrow as pa
except ImportError:  # pragma: no cover
    pa = None

_META_KEY = b'flowengine.type'


def require():
    if pa is None:
        raise DataError('pyarrow is not installed; install requirements.txt to use Arrow/Parquet features')
    return pa


def _arrow_type(f: Field):
    t = f.type
    return {
        FieldType.BOOL: pa.bool_(), FieldType.BYTE: pa.uint8(), FieldType.INT16: pa.int16(),
        FieldType.INT32: pa.int32(), FieldType.INT64: pa.int64(), FieldType.FLOAT: pa.float32(),
        FieldType.DOUBLE: pa.float64(), FieldType.DATE: pa.date32(), FieldType.TIME: pa.time64('us'),
        FieldType.DATETIME: pa.timestamp('us'), FieldType.BLOB: pa.binary(),
    }.get(t) or (
        (pa.decimal128(f.size, f.scale) if f.size <= 38 else pa.decimal256(f.size, f.scale))
        if t is FieldType.FIXED_DECIMAL else pa.string()
    )


def to_arrow(table: Table):
    require()
    fields, arrays = [], []
    for f, col in zip(table.schema, table.columns):
        at = _arrow_type(f)
        meta = {_META_KEY: f.type.value.encode(), b'size': str(f.size or '').encode(),
                b'scale': str(f.scale if f.scale is not None else '').encode()}
        fields.append(pa.field(f.name, at, nullable=True, metadata=meta))
        try:
            arrays.append(pa.array(col, type=at))
        except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError) as e:
            raise DataError(f'Cannot convert field {f.name!r} to Arrow {at}: {e}') from None
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def _field_from_arrow(af) -> Field:
    meta = af.metadata or {}
    if _META_KEY in meta:
        size = meta.get(b'size', b'').decode() or None
        scale = meta.get(b'scale', b'').decode() or None
        return Field(af.name, FieldType(meta[_META_KEY].decode()), int(size) if size else None,
                     int(scale) if scale is not None and scale != '' else None)
    t = af.type
    if pa.types.is_boolean(t):
        return Field(af.name, FieldType.BOOL)
    if pa.types.is_uint8(t):
        return Field(af.name, FieldType.BYTE)
    if pa.types.is_int8(t) or pa.types.is_int16(t):
        return Field(af.name, FieldType.INT16)
    if pa.types.is_int32(t) or pa.types.is_uint16(t):
        return Field(af.name, FieldType.INT32)
    if pa.types.is_integer(t):
        return Field(af.name, FieldType.INT64)
    if pa.types.is_float32(t):
        return Field(af.name, FieldType.FLOAT)
    if pa.types.is_floating(t):
        return Field(af.name, FieldType.DOUBLE)
    if pa.types.is_decimal(t):
        return Field(af.name, FieldType.FIXED_DECIMAL, t.precision, t.scale)
    if pa.types.is_date(t):
        return Field(af.name, FieldType.DATE)
    if pa.types.is_time(t):
        return Field(af.name, FieldType.TIME)
    if pa.types.is_timestamp(t):
        return Field(af.name, FieldType.DATETIME)
    if pa.types.is_binary(t) or pa.types.is_large_binary(t):
        return Field(af.name, FieldType.BLOB)
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return Field(af.name, FieldType.V_WSTRING, 2**30 - 1)
    return Field(af.name, FieldType.V_WSTRING, 2**30 - 1)  # nested/other -> text (JSON-like repr)


def from_arrow(at) -> Table:
    require()
    fields = [_field_from_arrow(f) for f in at.schema]
    cols = []
    for f, af, col in zip(fields, at.schema, at.columns):
        values = col.to_pylist()
        if f.type is FieldType.V_WSTRING and not (pa.types.is_string(af.type) or pa.types.is_large_string(af.type)):
            import json
            values = [None if v is None else (v if isinstance(v, str) else json.dumps(v, default=str)) for v in values]
        if f.type is FieldType.DATETIME:
            values = [None if v is None else v.replace(tzinfo=None) for v in values]
        if f.type is FieldType.FIXED_DECIMAL:
            values = [None if v is None else Decimal(v) for v in values]
        cols.append(values)
    try:
        return Table(Schema(fields), cols) if fields else Table([], [])
    except SchemaError as e:
        raise DataError(f'Arrow data cannot be represented: {e.message}') from None


def read_parquet(path) -> Table:
    require()
    import pyarrow.parquet as pq
    return from_arrow(pq.read_table(path))


def write_parquet(table: Table, path) -> None:
    require()
    import pyarrow.parquet as pq
    pq.write_table(to_arrow(table), path)


def read_ipc(path) -> Table:
    require()
    import pyarrow.feather as feather
    return from_arrow(feather.read_table(path))


def write_ipc(table: Table, path) -> None:
    require()
    import pyarrow.feather as feather
    feather.write_feather(to_arrow(table), path, compression='uncompressed')
