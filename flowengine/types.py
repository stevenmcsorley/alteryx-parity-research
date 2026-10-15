"""Independent field type system.

Type names follow the *publicly documented* data-type vocabulary of the
reference product so that tool contracts can be compared, but every rule
below (parsing grammar, overflow, truncation, formatting) is this engine's own
documented policy. Policies not yet compared against a reference are listed in
``docs/ENGINE_SEMANTICS.md`` as unverified.

Runtime value representation (``None`` is null for every type):

=============== ==========================
type            Python value
=============== ==========================
bool            bool
byte/int16/32/64 int (range-checked)
fixed_decimal   decimal.Decimal (size=precision, scale)
float/double    float
string family   str (size = max characters)
date            datetime.date
time            datetime.time
datetime        datetime.datetime (naive)
blob            bytes
=============== ==========================
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext
from enum import Enum

from .errors import SchemaError


class FieldType(str, Enum):
    BOOL = 'bool'
    BYTE = 'byte'
    INT16 = 'int16'
    INT32 = 'int32'
    INT64 = 'int64'
    FIXED_DECIMAL = 'fixed_decimal'
    FLOAT = 'float'
    DOUBLE = 'double'
    STRING = 'string'
    WSTRING = 'wstring'
    V_STRING = 'v_string'
    V_WSTRING = 'v_wstring'
    DATE = 'date'
    TIME = 'time'
    DATETIME = 'datetime'
    BLOB = 'blob'

    @property
    def is_integer(self) -> bool:
        return self in _INT_RANGES

    @property
    def is_numeric(self) -> bool:
        return self.is_integer or self in (FieldType.FIXED_DECIMAL, FieldType.FLOAT, FieldType.DOUBLE)

    @property
    def is_string(self) -> bool:
        return self in (FieldType.STRING, FieldType.WSTRING, FieldType.V_STRING, FieldType.V_WSTRING)

    @property
    def is_temporal(self) -> bool:
        return self in (FieldType.DATE, FieldType.TIME, FieldType.DATETIME)


_INT_RANGES = {
    FieldType.BYTE: (0, 255),
    FieldType.INT16: (-2**15, 2**15 - 1),
    FieldType.INT32: (-2**31, 2**31 - 1),
    FieldType.INT64: (-2**63, 2**63 - 1),
}
# Numeric widening order used when unifying schemas (e.g. Union) and in formulas.
_NUMERIC_RANK = [FieldType.BOOL, FieldType.BYTE, FieldType.INT16, FieldType.INT32, FieldType.INT64,
                 FieldType.FIXED_DECIMAL, FieldType.FLOAT, FieldType.DOUBLE]
_STRING_RANK = [FieldType.STRING, FieldType.V_STRING, FieldType.WSTRING, FieldType.V_WSTRING]

DEFAULT_STRING_SIZE = 254
DEFAULT_DECIMAL = (19, 6)


@dataclass(frozen=True)
class Field:
    name: str
    type: FieldType
    size: int | None = None
    scale: int | None = None
    description: str | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise SchemaError('Field names must be non-empty strings')
        object.__setattr__(self, 'type', FieldType(self.type))
        if self.type.is_string and self.size is None:
            object.__setattr__(self, 'size', DEFAULT_STRING_SIZE)
        if self.type is FieldType.FIXED_DECIMAL:
            if self.size is None:
                object.__setattr__(self, 'size', DEFAULT_DECIMAL[0])
            if self.scale is None:
                object.__setattr__(self, 'scale', DEFAULT_DECIMAL[1])
            if not (1 <= self.size <= 50 and 0 <= self.scale < self.size):
                raise SchemaError(f'Invalid fixed_decimal precision/scale for {self.name!r}')
        if self.size is not None and self.size < 1:
            raise SchemaError(f'Field size must be positive for {self.name!r}')

    def renamed(self, name: str) -> 'Field':
        return replace(self, name=name)

    def to_dict(self) -> dict:
        d = {'name': self.name, 'type': self.type.value}
        if self.size is not None:
            d['size'] = self.size
        if self.scale is not None:
            d['scale'] = self.scale
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'Field':
        return cls(d['name'], FieldType(d['type']), d.get('size'), d.get('scale'), d.get('description'))


class Schema(tuple):
    """Ordered, name-unique tuple of fields. Field names are case-sensitive."""

    def __new__(cls, fields=()):
        fields = tuple(fields)
        names = [f.name for f in fields]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise SchemaError(f'Duplicate field names: {dupes}')
        return super().__new__(cls, fields)

    @property
    def names(self) -> list[str]:
        return [f.name for f in self]

    def index(self, name: str) -> int:  # type: ignore[override]
        for i, f in enumerate(self):
            if f.name == name:
                return i
        raise SchemaError(f'Unknown field {name!r}; available: {self.names}')

    def field(self, name: str) -> Field:
        return self[self.index(name)]

    def __contains__(self, name) -> bool:  # name-based membership
        if isinstance(name, Field):
            return tuple.__contains__(self, name)
        return any(f.name == name for f in self)

    def to_list(self) -> list[dict]:
        return [f.to_dict() for f in self]


# --------------------------------------------------------------------------- parsing

_INT_RE = re.compile(r'^[+-]?\d+$')
_NUM_RE = re.compile(r'^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$')
_DATE_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
_TIME_RE = re.compile(r'^(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$')
_DT_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$')
_TRUE = {'true', 't', 'yes', 'y', '1'}
_FALSE = {'false', 'f', 'no', 'n', '0'}


class CastFailure(Exception):
    """Internal signal: value cannot be represented in the target type."""


def _frac(us: str | None) -> int:
    return int((us or '0').ljust(6, '0'))


def parse_date(text: str) -> _dt.date:
    m = _DATE_RE.match(text)
    if not m:
        raise CastFailure(f'{text!r} is not an ISO date (YYYY-MM-DD)')
    try:
        return _dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError as e:
        raise CastFailure(f'{text!r}: {e}') from None


def parse_time(text: str) -> _dt.time:
    m = _TIME_RE.match(text)
    if not m:
        raise CastFailure(f'{text!r} is not a time (HH:MM[:SS[.ffffff]])')
    try:
        return _dt.time(int(m[1]), int(m[2]), int(m[3] or 0), _frac(m[4]))
    except ValueError as e:
        raise CastFailure(f'{text!r}: {e}') from None


def parse_datetime(text: str) -> _dt.datetime:
    m = _DT_RE.match(text)
    if not m:
        try:
            d = parse_date(text)
        except CastFailure:
            raise CastFailure(f'{text!r} is not a datetime (YYYY-MM-DD HH:MM:SS)') from None
        return _dt.datetime(d.year, d.month, d.day)
    try:
        return _dt.datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6] or 0), _frac(m[7]))
    except ValueError as e:
        raise CastFailure(f'{text!r}: {e}') from None


def format_value(value, double_text: str = 'g15') -> str | None:
    """Canonical text rendering used by CSV output and string conversion.
    ``double_text`` comes from the compatibility profile ('g15' or 'shortest')."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 'True' if value else 'False'
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return repr(value)
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        # 15 significant digits, as the reference renders doubles [ref:formula-rounding,
        # summarize-statistics]; exponent form beyond that range is engine policy (unverified)
        return repr(value) if double_text == 'shortest' else format(value, '.15g')
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, _dt.datetime):
        return value.isoformat(sep=' ', timespec='microseconds' if value.microsecond else 'seconds')
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _to_decimal(value) -> Decimal:
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, (int, Decimal)):
        d = Decimal(value)
        if not d.is_finite():
            raise CastFailure(f'{value!r} is not a finite number')
        return d
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise CastFailure(f'{value!r} has no decimal representation')
        return Decimal(repr(value))
    if isinstance(value, str):
        text = value.strip()
        if not _NUM_RE.match(text):
            raise CastFailure(f'{value!r} is not a number')
        return Decimal(text)
    raise CastFailure(f'Cannot convert {type(value).__name__} to a number')


def cast_value(value, field: Field):
    """Convert ``value`` to ``field``'s representation.

    Returns ``(converted, warning_or_None)``. A non-representable value raises
    :class:`CastFailure`; the caller decides (via its error policy) whether that
    becomes null-with-warning or a hard error. Truncation of over-long strings is
    a warning, not a failure.
    """
    if value is None:
        return None, None
    t = field.type
    if t is FieldType.BOOL:
        if isinstance(value, bool):
            return value, None
        if isinstance(value, (int, float, Decimal)):
            return value != 0, None
        if isinstance(value, str):
            low = value.strip().lower()
            if low in _TRUE:
                return True, None
            if low in _FALSE:
                return False, None
        raise CastFailure(f'{value!r} is not a boolean')
    if t.is_integer:
        if isinstance(value, bool):
            n = int(value)
        elif isinstance(value, int):
            n = value
        elif isinstance(value, str) and _INT_RE.match(value.strip()):
            n = int(value.strip())
        elif isinstance(value, (float, Decimal)):
            # numeric -> integer rounds half away from zero [ref:probe-select-int-rounding]
            d = _to_decimal(value)
            n = int(d.to_integral_value(rounding=ROUND_HALF_UP))
        else:
            d = _to_decimal(value)  # text must denote a whole number (engine policy)
            if d != d.to_integral_value():
                raise CastFailure(f'{value!r} is not a whole number')
            n = int(d)
        lo, hi = _INT_RANGES[t]
        if not lo <= n <= hi:
            raise CastFailure(f'{n} overflows {t.value} range [{lo}, {hi}]')
        return n, None
    if t is FieldType.FIXED_DECIMAL:
        d = _to_decimal(value)
        if not d.is_finite():
            raise CastFailure(f'{value!r} is not a finite fixed-decimal value')
        with localcontext() as ctx:
            ctx.prec = 60
            try:
                q = d.quantize(Decimal(1).scaleb(-field.scale), rounding=ROUND_HALF_EVEN)
            except InvalidOperation:
                raise CastFailure(f'{value!r} cannot be represented as fixed_decimal({field.size},{field.scale})') from None
        digits = len(q.as_tuple().digits)
        if digits > field.size:
            raise CastFailure(f'{value!r} exceeds fixed_decimal({field.size},{field.scale})')
        return q, None
    if t in (FieldType.FLOAT, FieldType.DOUBLE):
        try:
            if isinstance(value, (bool, int, float, Decimal)):
                f = float(value)
            elif isinstance(value, str):
                text = value.strip()
                if not _NUM_RE.match(text):
                    raise CastFailure(f'{value!r} is not a number')
                f = float(text)
            else:
                raise CastFailure(f'Cannot convert {type(value).__name__} to {t.value}')
        except (OverflowError, ValueError):
            raise CastFailure(f'Value overflows {t.value}') from None
        if isinstance(value, (int, Decimal)) and not isinstance(value, bool) and math.isinf(f):
            raise CastFailure(f'Value overflows {t.value}')
        if t is FieldType.FLOAT and math.isfinite(f) and abs(f) > 3.4028234663852886e38:
            raise CastFailure(f'{value!r} overflows float')
        return f, None
    if t.is_string:
        text = value if isinstance(value, str) else format_value(value)
        if field.size is not None and len(text) > field.size:
            return text[:field.size], f'truncated to {field.size} characters'
        return text, None
    if t is FieldType.DATE:
        if isinstance(value, _dt.datetime):
            return value.date(), None
        if isinstance(value, _dt.date):
            return value, None
        if isinstance(value, str):
            text = value.strip()
            if _DT_RE.match(text):
                return parse_datetime(text).date(), None
            return parse_date(text), None
        raise CastFailure(f'Cannot convert {type(value).__name__} to date')
    if t is FieldType.TIME:
        if isinstance(value, _dt.datetime):
            return value.time(), None
        if isinstance(value, _dt.time):
            return value, None
        if isinstance(value, str):
            return parse_time(value.strip()), None
        raise CastFailure(f'Cannot convert {type(value).__name__} to time')
    if t is FieldType.DATETIME:
        if isinstance(value, _dt.datetime):
            return value, None
        if isinstance(value, _dt.date):
            return _dt.datetime(value.year, value.month, value.day), None
        if isinstance(value, str):
            return parse_datetime(value.strip()), None
        raise CastFailure(f'Cannot convert {type(value).__name__} to datetime')
    if t is FieldType.BLOB:
        if isinstance(value, bytes):
            return value, None
        if isinstance(value, str):
            return value.encode('utf-8'), None
        raise CastFailure(f'Cannot convert {type(value).__name__} to blob')
    raise CastFailure(f'Unsupported type {t}')  # pragma: no cover


def infer_type(value) -> FieldType:
    """Type of a Python runtime value (used by formulas and Text Input inference)."""
    if isinstance(value, bool):
        return FieldType.BOOL
    if isinstance(value, int):
        return FieldType.INT64
    if isinstance(value, Decimal):
        return FieldType.FIXED_DECIMAL
    if isinstance(value, float):
        return FieldType.DOUBLE
    if isinstance(value, _dt.datetime):
        return FieldType.DATETIME
    if isinstance(value, _dt.date):
        return FieldType.DATE
    if isinstance(value, _dt.time):
        return FieldType.TIME
    if isinstance(value, bytes):
        return FieldType.BLOB
    return FieldType.V_WSTRING


def unify_fields(a: Field, b: Field) -> Field:
    """Smallest common field able to hold values of both (used by Union)."""
    if a.type is FieldType.FIXED_DECIMAL and b.type is FieldType.FIXED_DECIMAL:
        scale = max(a.scale, b.scale)
        precision = max(a.size - a.scale, b.size - b.scale) + scale
        if precision > 50:
            raise SchemaError(
                f'Cannot unify fixed_decimal fields for {a.name!r} without exceeding '
                f'precision 50 (required {precision})'
            )
        return Field(a.name, FieldType.FIXED_DECIMAL, max(1, precision), scale)
    if a.type == b.type:
        size = max(a.size, b.size) if a.size is not None and b.size is not None else (a.size or b.size)
        scale = max(a.scale, b.scale) if a.scale is not None and b.scale is not None else (a.scale or b.scale)
        return Field(a.name, a.type, size, scale)
    if a.type in _NUMERIC_RANK and b.type in _NUMERIC_RANK:
        # Keep exact decimal/integer combinations exact. Promoting these to a
        # binary float silently changes values above 2**53 and decimal cents.
        exact_numeric = {FieldType.BOOL, FieldType.BYTE, FieldType.INT16, FieldType.INT32,
                         FieldType.INT64, FieldType.FIXED_DECIMAL}
        if a.type in exact_numeric and b.type in exact_numeric and (
                a.type is FieldType.FIXED_DECIMAL or b.type is FieldType.FIXED_DECIMAL):
            integer_digits = {
                FieldType.BOOL: 1, FieldType.BYTE: 3, FieldType.INT16: 5,
                FieldType.INT32: 10, FieldType.INT64: 19,
            }
            decimals = [f for f in (a, b) if f.type is FieldType.FIXED_DECIMAL]
            scale = max(f.scale for f in decimals)
            whole_digits = max(
                [f.size - f.scale for f in decimals]
                + [integer_digits[f.type] for f in (a, b) if f.type in integer_digits]
            )
            precision = max(1, whole_digits) + scale
            if precision > 50:
                raise SchemaError(
                    f'Cannot unify {a.type.value} and {b.type.value} for {a.name!r} '
                    f'without exceeding fixed_decimal precision 50 (required {precision})'
                )
            return Field(a.name, FieldType.FIXED_DECIMAL, precision, scale)
        if (a.type is FieldType.FIXED_DECIMAL and b.type in (FieldType.FLOAT, FieldType.DOUBLE)) or (
                b.type is FieldType.FIXED_DECIMAL and a.type in (FieldType.FLOAT, FieldType.DOUBLE)):
            raise SchemaError(
                f'Cannot safely unify fixed_decimal with binary floating point for {a.name!r}; '
                'convert explicitly after choosing an acceptable precision policy'
            )
        if FieldType.INT64 in (a.type, b.type) and any(
                f.type in (FieldType.FLOAT, FieldType.DOUBLE) for f in (a, b)):
            raise SchemaError(
                f'Cannot safely unify int64 with binary floating point for {a.name!r}; '
                'values above 2**53 may lose integer precision'
            )
        hi = max(a.type, b.type, key=_NUMERIC_RANK.index)
        if hi is FieldType.FIXED_DECIMAL:
            return Field(a.name, FieldType.DOUBLE)
        if hi is FieldType.FLOAT:
            hi = FieldType.DOUBLE
        return Field(a.name, hi)
    if a.type.is_string and b.type.is_string:
        hi = max(a.type, b.type, key=_STRING_RANK.index)
        return Field(a.name, hi, max(a.size or 0, b.size or 0) or None)
    if {a.type, b.type} == {FieldType.DATE, FieldType.DATETIME}:
        return Field(a.name, FieldType.DATETIME)
    return Field(a.name, FieldType.V_WSTRING, max(a.size or DEFAULT_STRING_SIZE, b.size or DEFAULT_STRING_SIZE))


def type_for_values(values) -> FieldType:
    """Narrowest common type for a column of Python values (nulls ignored)."""
    types = {infer_type(v) for v in values if v is not None}
    if not types:
        return FieldType.V_WSTRING
    if len(types) == 1:
        return types.pop()
    if types <= set(_NUMERIC_RANK):
        return max(types, key=_NUMERIC_RANK.index) if FieldType.FIXED_DECIMAL not in types else FieldType.DOUBLE
    return FieldType.V_WSTRING
