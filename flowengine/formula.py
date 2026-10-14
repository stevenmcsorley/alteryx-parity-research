"""Independent expression language: lexer, parser, static typing and compiler.

Syntax is modelled on the publicly documented style of workflow-tool formula
expressions (``[Field]`` references, ``IF .. THEN .. ELSEIF .. ELSE .. ENDIF``,
named functions). The grammar, function signatures and null/error semantics are
this engine's own and are specified in ``docs/ENGINE_SEMANTICS.md``.

Precedence (low -> high)::

    OR ||  <  AND &&  <  NOT !  <  comparison / IN  <  + -  <  * / %  <  unary -  <  primary

Null and comparison policy. Items marked [ref:<case>] were observed on the
reference engine by the differential harness (conformance/cases.py); the rest
are engine policy, listed as unverified in docs/ENGINE_SEMANTICS.md.
* arithmetic propagates null [ref:probe-null-arithmetic-bool];
* string ``+`` treats a single null operand as empty; null + null is null
  [ref:probe-null-string-functions, formula-null-concat];
* ``=``/``!=`` treat null as a comparable value (null = null is true);
  ordering comparisons with a null operand are false [ref:probe-null-arithmetic-bool];
* string comparisons (=, !=, <, <=, >, >=, IN) are case-insensitive
  [ref:filter-string-eq-case, probe-string-ordering];
* boolean contexts (AND/OR/NOT, IF, Filter) treat null as false;
* division by zero yields null and records a warning [ref:formula-divide-by-zero].
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from .errors import ExpressionError
from .types import FieldType, Schema, format_value, parse_date, parse_datetime, parse_time, CastFailure

# =========================================================================== lexer

_TOKEN_SPEC = [
    ('WS', r'\s+'),
    ('LINE_COMMENT', r'//[^\n]*'),
    ('BLOCK_COMMENT', r'/\*.*?\*/'),
    ('NUMBER', r'(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?'),
    ('STRING', r'"(?:[^"\\]|\\.|"")*"|\'(?:[^\'\\]|\\.|\'\')*\''),
    ('FIELD', r'\[[^\]]+\]'),
    ('OP', r'==|!=|<>|<=|>=|&&|\|\||[=<>+\-*/%!(),]'),
    ('IDENT', r'[A-Za-z_][A-Za-z0-9_]*'),
]
_TOKEN_RE = re.compile('|'.join(f'(?P<{n}>{p})' for n, p in _TOKEN_SPEC), re.S)
_KEYWORDS = {'IF', 'THEN', 'ELSEIF', 'ELSE', 'ENDIF', 'AND', 'OR', 'NOT', 'IN', 'TRUE', 'FALSE', 'NULL'}
_ROW_REF = re.compile(r'^Row([+-]\d+):(.+)$')


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    pos: int


def tokenize(src: str) -> list[Token]:
    if not isinstance(src, str):
        raise ExpressionError('Expression must be a string')
    out, pos = [], 0
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if not m:
            raise ExpressionError(f'Unexpected character {src[pos]!r}', position=pos)
        kind = m.lastgroup
        text = m.group()
        if kind not in ('WS', 'LINE_COMMENT', 'BLOCK_COMMENT'):
            if kind == 'IDENT' and text.upper() in _KEYWORDS:
                kind, text = 'KW', text.upper()
            out.append(Token(kind, text, pos))
        pos = m.end()
    out.append(Token('EOF', '', len(src)))
    return out


def _unquote(text: str) -> str:
    q = text[0]
    body = text[1:-1].replace(q + q, q)
    return re.sub(r'\\(.)', lambda m: {'n': '\n', 't': '\t', 'r': '\r'}.get(m[1], m[1]), body)


# =========================================================================== AST

@dataclass(frozen=True)
class Node:
    pos: int


@dataclass(frozen=True)
class Literal(Node):
    value: Any


@dataclass(frozen=True)
class FieldRef(Node):
    name: str
    row_offset: int = 0


@dataclass(frozen=True)
class Unary(Node):
    op: str
    operand: Node


@dataclass(frozen=True)
class Binary(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class InList(Node):
    operand: Node
    items: tuple
    negated: bool


@dataclass(frozen=True)
class Call(Node):
    name: str
    args: tuple


@dataclass(frozen=True)
class Conditional(Node):
    branches: tuple  # ((cond, value), ...)
    otherwise: Node


class _Parser:
    def __init__(self, src: str):
        self.tokens = tokenize(src)
        self.i = 0

    def peek(self, offset=0) -> Token:
        return self.tokens[min(self.i + offset, len(self.tokens) - 1)]

    def advance(self) -> Token:
        t = self.tokens[self.i]
        self.i += 1
        return t

    def accept(self, kind, value=None):
        t = self.peek()
        if t.kind == kind and (value is None or t.value == value):
            return self.advance()
        return None

    def expect(self, kind, value=None, what=None):
        t = self.accept(kind, value)
        if not t:
            got = self.peek()
            raise ExpressionError(f'Expected {what or value or kind}, found {got.value or "end of expression"!r}',
                                  position=got.pos)
        return t

    def parse(self) -> Node:
        if self.peek().kind == 'EOF':
            raise ExpressionError('Empty expression', position=0)
        node = self.or_expr()
        if self.peek().kind != 'EOF':
            t = self.peek()
            raise ExpressionError(f'Unexpected {t.value!r}', position=t.pos)
        return node

    def or_expr(self):
        left = self.and_expr()
        while (t := self.accept('KW', 'OR') or self.accept('OP', '||')):
            left = Binary(t.pos, 'OR', left, self.and_expr())
        return left

    def and_expr(self):
        left = self.not_expr()
        while (t := self.accept('KW', 'AND') or self.accept('OP', '&&')):
            left = Binary(t.pos, 'AND', left, self.not_expr())
        return left

    def not_expr(self):
        if (t := self.accept('KW', 'NOT') or self.accept('OP', '!')):
            return Unary(t.pos, 'NOT', self.not_expr())
        return self.comparison()

    def comparison(self):
        left = self.additive()
        t = self.peek()
        if t.kind == 'OP' and t.value in ('=', '==', '!=', '<>', '<', '<=', '>', '>='):
            self.advance()
            op = {'==': '=', '<>': '!='}.get(t.value, t.value)
            return Binary(t.pos, op, left, self.additive())
        negated = False
        if t.kind == 'KW' and t.value == 'NOT' and self.peek(1).kind == 'KW' and self.peek(1).value == 'IN':
            self.advance()
            negated = True
        if (tin := self.accept('KW', 'IN')):
            self.expect('OP', '(')
            items = [self.or_expr()]
            while self.accept('OP', ','):
                items.append(self.or_expr())
            self.expect('OP', ')')
            return InList(tin.pos, left, tuple(items), negated)
        return left

    def additive(self):
        left = self.multiplicative()
        while (t := self.peek()).kind == 'OP' and t.value in ('+', '-'):
            self.advance()
            left = Binary(t.pos, t.value, left, self.multiplicative())
        return left

    def multiplicative(self):
        left = self.unary()
        while (t := self.peek()).kind == 'OP' and t.value in ('*', '/', '%'):
            self.advance()
            left = Binary(t.pos, t.value, left, self.unary())
        return left

    def unary(self):
        if (t := self.accept('OP', '-')):
            return Unary(t.pos, 'NEG', self.unary())
        if self.accept('OP', '+'):
            return self.unary()
        return self.primary()

    def primary(self):
        t = self.peek()
        if t.kind == 'NUMBER':
            self.advance()
            text = t.value
            return Literal(t.pos, int(text) if re.fullmatch(r'\d+', text) else float(text))
        if t.kind == 'STRING':
            self.advance()
            return Literal(t.pos, _unquote(t.value))
        if t.kind == 'FIELD':
            self.advance()
            inner = t.value[1:-1]
            m = _ROW_REF.match(inner)
            if m:
                return FieldRef(t.pos, m[2], int(m[1]))
            return FieldRef(t.pos, inner)
        if t.kind == 'KW':
            if t.value in ('TRUE', 'FALSE'):
                self.advance()
                return Literal(t.pos, t.value == 'TRUE')
            if t.value == 'NULL':
                self.advance()
                if self.accept('OP', '('):
                    self.expect('OP', ')')
                return Literal(t.pos, None)
            if t.value == 'IF':
                return self.conditional()
        if t.kind == 'IDENT' and not (self.peek(1).kind == 'OP' and self.peek(1).value == '('):
            # a bare identifier is a field reference (accepted by the reference, e.g. Generate Rows conditions)
            self.advance()
            return FieldRef(t.pos, t.value)
        if t.kind == 'IDENT':
            self.advance()
            self.expect('OP', '(', what=f"'(' after function name {t.value}")
            args = []
            if not self.accept('OP', ')'):
                args.append(self.or_expr())
                while self.accept('OP', ','):
                    args.append(self.or_expr())
                self.expect('OP', ')')
            return Call(t.pos, t.value.upper(), tuple(args))
        if self.accept('OP', '('):
            node = self.or_expr()
            self.expect('OP', ')')
            return node
        raise ExpressionError(f'Unexpected {t.value or "end of expression"!r}', position=t.pos)

    def conditional(self):
        start = self.expect('KW', 'IF')
        branches = []
        cond = self.or_expr()
        self.expect('KW', 'THEN')
        branches.append((cond, self.or_expr()))
        while self.accept('KW', 'ELSEIF'):
            cond = self.or_expr()
            self.expect('KW', 'THEN')
            branches.append((cond, self.or_expr()))
        self.expect('KW', 'ELSE', what='ELSE (every IF requires an ELSE branch)')
        otherwise = self.or_expr()
        self.expect('KW', 'ENDIF')
        return Conditional(start.pos, tuple(branches), otherwise)


def parse(src: str) -> Node:
    return _Parser(src).parse()


# =========================================================================== static typing
# Value kinds used for static checking: bool, number, string, date, time,
# datetime, blob, null (literal NULL), any (unknown/polymorphic).

def kind_of_field(ftype: FieldType) -> str:
    if ftype is FieldType.BOOL:
        return 'bool'
    if ftype.is_numeric:
        return 'number'
    if ftype.is_string:
        return 'string'
    return {FieldType.DATE: 'date', FieldType.TIME: 'time', FieldType.DATETIME: 'datetime',
            FieldType.BLOB: 'blob'}[ftype]


def _compatible(a: str, b: str) -> bool:
    if 'any' in (a, b) or 'null' in (a, b) or a == b:
        return True
    return {a, b} <= {'date', 'datetime', 'string'}  # dates compare with ISO strings & each other


# =========================================================================== runtime helpers

class EvalContext:
    """Per-evaluation services: clock, warnings sink and multi-row access."""

    def __init__(self, *, now: Callable[[], _dt.datetime] | None = None, warn: Callable[[str], None] | None = None):
        self.now = now or _dt.datetime.now
        self._warn = warn
        self.warnings: dict[str, int] = {}
        self.row_accessor: Callable[[int, int], Any] | None = None

    def warn(self, message: str):
        self.warnings[message] = self.warnings.get(message, 0) + 1
        if self._warn and self.warnings[message] == 1:
            self._warn(message)


def _arith(op, a, b, ctx):
    if op == '+' and (isinstance(a, str) or isinstance(b, str)) and (a is None or b is None):
        return a if b is None else b  # null + "x" -> "x"
    if a is None or b is None:
        return None
    if isinstance(a, bool) or isinstance(b, bool):
        a, b = (int(a) if isinstance(a, bool) else a), (int(b) if isinstance(b, bool) else b)
    if isinstance(a, float) and isinstance(b, Decimal):
        b = float(b)
    elif isinstance(b, float) and isinstance(a, Decimal):
        a = float(a)
    if op == '+':
        if isinstance(a, str) and isinstance(b, str):
            return a + b
        if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
            return a + b
    elif op == '-':
        if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
            return a - b
    elif op == '*':
        if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
            return a * b
    elif op in ('/', '%'):
        if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
            if b == 0:
                ctx.warn('division by zero produced null')
                return None
            if op == '/':
                return (a / b) if isinstance(a, Decimal) or isinstance(b, Decimal) else a / b
            return _int_mod(a, b, ctx)
    raise ExpressionError(f'Operator {op!r} cannot combine {type(a).__name__} and {type(b).__name__}')


def _int_mod(a, b, ctx):
    """Integer modulo of operands truncated toward zero; sign follows the dividend
    [ref:formula-rounding]: Mod(2.5, 3) = 2, Mod(-7, 3) = -1."""
    ia, ib = int(a), int(b)
    if ib == 0:
        ctx.warn('division by zero produced null')
        return None
    r = abs(ia) % abs(ib)
    return r if ia >= 0 else -r


def _coerce_cmp(a, b):
    """Allow date/datetime and ISO-string comparisons; otherwise require like types."""
    if isinstance(a, _dt.datetime) and not isinstance(b, _dt.datetime):
        if isinstance(b, _dt.date):
            b = _dt.datetime(b.year, b.month, b.day)
        elif isinstance(b, str):
            b = parse_datetime(b.strip())
    elif isinstance(b, _dt.datetime) and not isinstance(a, _dt.datetime):
        a, b = _coerce_cmp(b, a)[::-1]
        return a, b
    elif isinstance(a, _dt.date) and isinstance(b, str):
        b = parse_date(b.strip()[:10])
    elif isinstance(b, _dt.date) and isinstance(a, str):
        a = parse_date(a.strip()[:10])
    if isinstance(a, float) and isinstance(b, Decimal):
        b = float(b)
    elif isinstance(b, float) and isinstance(a, Decimal):
        a = float(a)
    return a, b


def _compare(op, a, b):
    if a is None or b is None:
        if op == '=':
            return a is None and b is None
        if op == '!=':
            return not (a is None and b is None)
        return False
    try:
        a, b = _coerce_cmp(a, b)
    except CastFailure as e:
        raise ExpressionError(f'Cannot compare values: {e}') from None
    num = (int, float, Decimal, bool)
    if not (isinstance(a, num) and isinstance(b, num)) and type(a) is not type(b):
        raise ExpressionError(f'Cannot compare {type(a).__name__} with {type(b).__name__}')
    if isinstance(a, str) and isinstance(b, str):
        a, b = a.lower(), b.lower()
    if op == '=':
        return a == b
    if op == '!=':
        return a != b
    if op == '<':
        return a < b
    if op == '<=':
        return a <= b
    if op == '>':
        return a > b
    return a >= b


def truthy(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        return v != 0
    raise ExpressionError(f'Expected a boolean condition, got {type(v).__name__} {format_value(v)!r}')


# =========================================================================== functions

def _s(v):
    return v if isinstance(v, str) else format_value(v)


def _to_number(ctx, v):
    if v is None:
        return 0  # [ref:formula-tonumber]
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float, Decimal)):
        return v
    text = str(v).strip()
    if re.fullmatch(r'[+-]?\d+', text):
        return int(text)
    try:
        return float(text) if text and re.fullmatch(r'[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?', text) else _bad_number(text, ctx)
    except ValueError:
        return _bad_number(text, ctx)


def _bad_number(text, ctx):
    ctx.warn('ToNumber: non-numeric text converted to 0')
    return 0  # [ref:formula-tonumber]


def _round(x, mult):
    """floor(x / mult + 0.5) * mult in binary floating point [ref:formula-rounding]:
    Round(2.5, 1) = 3, Round(-2.5, 1) = -2, Round(1.45, 0.1) = 1.4."""
    if x is None or mult is None:
        return None
    if mult == 0:
        raise ExpressionError('Round multiple must be non-zero')
    r = math.floor(float(x) / float(mult) + 0.5) * float(mult)
    if isinstance(x, int) and isinstance(mult, int) and not isinstance(x, bool):
        return int(r)
    return r


_DT_UNITS = {
    'year': 'years', 'years': 'years', 'month': 'months', 'months': 'months',
    'day': 'days', 'days': 'days', 'hour': 'hours', 'hours': 'hours',
    'minute': 'minutes', 'minutes': 'minutes', 'second': 'seconds', 'seconds': 'seconds',
}


def _as_dt(v):
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v
    if isinstance(v, _dt.date):
        return _dt.datetime(v.year, v.month, v.day)
    if isinstance(v, str):
        try:
            return parse_datetime(v.strip())
        except CastFailure as e:
            raise ExpressionError(str(e)) from None
    raise ExpressionError(f'Expected a date/datetime, got {type(v).__name__}')


def _add_months(d: _dt.datetime, n: int) -> _dt.datetime:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(y, m)[1])
    return d.replace(year=y, month=m, day=day)


def _dt_add(v, n, unit):
    d = _as_dt(v)
    if d is None or n is None or unit is None:
        return None
    u = _DT_UNITS.get(str(unit).strip().lower())
    if not u:
        raise ExpressionError(f'Unknown date unit {unit!r}')
    n = int(n)
    if u == 'years':
        out = _add_months(d, 12 * n)
    elif u == 'months':
        out = _add_months(d, n)
    else:
        out = d + _dt.timedelta(**{u: n})
    return out.date() if isinstance(v, _dt.date) and not isinstance(v, _dt.datetime) and u in ('years', 'months', 'days') else out


def _dt_diff(a, b, unit):
    x, y = _as_dt(a), _as_dt(b)
    if x is None or y is None or unit is None:
        return None
    u = _DT_UNITS.get(str(unit).strip().lower())
    if not u:
        raise ExpressionError(f'Unknown date unit {unit!r}')
    if u in ('years', 'months'):
        months = (x.year - y.year) * 12 + (x.month - y.month)
        # whole months only: back off if the day/time has not been reached
        if months > 0 and (x.day, x.time()) < (y.day, y.time()):
            months -= 1
        elif months < 0 and (x.day, x.time()) > (y.day, y.time()):
            months += 1
        return months // 12 if u == 'years' and months >= 0 else (-((-months) // 12) if u == 'years' else months)
    secs = (x - y).total_seconds()
    per = {'days': 86400, 'hours': 3600, 'minutes': 60, 'seconds': 1}[u]
    return int(secs / per)  # truncation toward zero


_FMT_ALLOWED = set('YymdHMSbBaAjIpe%')


def _check_fmt(fmt):
    for m in re.finditer(r'%(.)', fmt):
        if m[1] not in _FMT_ALLOWED:
            raise ExpressionError(f'Unsupported date format specifier %{m[1]}')


def _dt_format(v, fmt):
    if v is None or fmt is None:
        return None
    _check_fmt(fmt)
    d = v if isinstance(v, (_dt.date, _dt.time)) else _as_dt(v)
    return d.strftime(fmt.replace('%e', '%d'))


def _dt_parse(ctx, text, fmt):
    if text is None or fmt is None:
        return None
    _check_fmt(fmt)
    try:
        d = _dt.datetime.strptime(text.strip(), fmt.replace('%e', '%d'))
    except ValueError:
        ctx.warn('DateTimeParse: text did not match format; produced null')
        return None
    return d.date() if not re.search(r'%[HMSIp]', fmt) else d


def _regex(pattern, case_insensitive):
    try:
        return re.compile(pattern, re.I if case_insensitive else 0)
    except re.error as e:
        raise ExpressionError(f'Invalid regular expression {pattern!r}: {e}') from None


def _null_guard(fn):
    def wrapper(ctx, *args):
        if any(a is None for a in args):
            return None
        return fn(ctx, *args)
    return wrapper


def _str_fn(fn):
    return _null_guard(lambda ctx, s, *rest: fn(_s(s), *rest))


def _str_fn_null_as_empty(fn):
    """Null input behaves as '' [ref:formula-strings, probe-null-string-functions]."""
    return lambda ctx, s, *rest: fn('' if s is None else _s(s), *rest)


# Third argument is a case-insensitive flag defaulting to 1; null text never
# matches [ref:probe-string-ordering, probe-contains-flag-and-codepoints, formula-strings].
def _contains(ctx, s, t, ci=1):
    s, t = ('' if s is None else _s(s)), ('' if t is None else _s(t))
    return (t.lower() in s.lower()) if truthy(ci) else (t in s)


def _starts(ctx, s, t, ci=1):
    s, t = ('' if s is None else _s(s)), ('' if t is None else _s(t))
    return s.lower().startswith(t.lower()) if truthy(ci) else s.startswith(t)


def _ends(ctx, s, t, ci=1):
    s, t = ('' if s is None else _s(s)), ('' if t is None else _s(t))
    return s.lower().endswith(t.lower()) if truthy(ci) else s.endswith(t)


def _substr(ctx, s, start, length=None):
    if s is None or start is None:
        return None
    s = _s(s)
    start = max(int(start), 0)
    return s[start:] if length is None else s[start:start + max(int(length), 0)]


def _trim(chars_side):
    def fn(ctx, s, chars=None):
        if s is None:
            return None
        s = _s(s)
        return getattr(s, chars_side)(chars) if chars is not None else getattr(s, chars_side)()
    return fn


def _pad(side):
    def fn(ctx, s, n, ch=' '):
        if s is None or n is None:
            return None
        ch = (_s(ch) or ' ')[0]
        return _s(s).rjust(int(n), ch) if side == 'L' else _s(s).ljust(int(n), ch)
    return fn


def _title(ctx, s):
    if s is None:
        return None
    return re.sub(r"[A-Za-z\u00C0-\uFFFF]+", lambda m: m[0][0].upper() + m[0][1:].lower(), _s(s))


def _replace(ctx, s, old, new):
    s = '' if s is None else _s(s)  # Replace(null, ..) is '' [ref:probe-null-string-functions]
    if old is None or old == '' or new is None:
        return s
    return s.replace(_s(old), _s(new))


def _findstring(ctx, s, t):
    if s is None or t is None:
        return -1  # [ref:formula-strings]
    return _s(s).find(_s(t))


def _rx_match(ctx, s, p, ci=0):
    if s is None or p is None:
        return None
    return _regex(p, truthy(ci)).fullmatch(_s(s)) is not None


def _rx_replace(ctx, s, p, r, ci=0):
    if s is None or p is None or r is None:
        return None
    rep = re.sub(r'\$(\d+)', r'\\g<\1>', r)
    return _regex(p, truthy(ci)).sub(rep, _s(s))


def _rx_count(ctx, s, p, ci=0):
    if s is None or p is None:
        return None
    return len(_regex(p, truthy(ci)).findall(_s(s)))


def _iif(ctx, c, a, b):
    return a if truthy(c) else b


def _to_string(ctx, v, decimals=None):
    if v is None:
        return ''  # [ref:probe-null-string-functions]
    if decimals is not None and isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return f'{float(v) if isinstance(v, float) else v:.{int(decimals)}f}'
    return format_value(v)


def _num_fn(fn):
    def wrapper(ctx, *args):
        if any(a is None for a in args):
            return None
        for a in args:
            if not isinstance(a, (int, float, Decimal)) or isinstance(a, bool):
                raise ExpressionError(f'Expected a number, got {type(a).__name__}')
        try:
            return fn(*args)
        except (ValueError, OverflowError) as e:
            ctx.warn(f'math domain error produced null ({e})')
            return None
    return wrapper


def _minmax(pick):
    def fn(ctx, *args):
        vals = [a for a in args if a is not None]
        if not vals:
            return None
        return pick(vals)
    return fn


def _date_part(attr):
    def fn(ctx, v):
        if v is None:
            return None
        d = v if isinstance(v, (_dt.date, _dt.time)) else _as_dt(v)
        return getattr(d, attr)
    return fn


def _to_date(ctx, v):
    if v is None:
        return None
    try:
        return v.date() if isinstance(v, _dt.datetime) else (v if isinstance(v, _dt.date) else parse_date(_s(v).strip()[:10]))
    except CastFailure:
        ctx.warn('ToDate: unparseable text produced null')
        return None


def _to_datetime(ctx, v):
    if v is None:
        return None
    try:
        return _as_dt(v)
    except ExpressionError:
        ctx.warn('ToDateTime: unparseable text produced null')
        return None


def _first_of_month(ctx, v):
    d = _as_dt(v)
    return None if d is None else d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _last_of_month(ctx, v):
    import calendar
    d = _as_dt(v)
    if d is None:
        return None
    return d.replace(day=calendar.monthrange(d.year, d.month)[1], hour=23, minute=59, second=59, microsecond=0)


def _is_number_text(ctx, v):
    if v is None:
        return False
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return True
    return isinstance(v, str) and bool(re.fullmatch(r'\s*[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?\s*', v))


def _count_words(ctx, s):
    return None if s is None else len(_s(s).split())


def _coalesce(ctx, *args):
    for a in args:
        if a is not None:
            return a
    return None


def _switch(ctx, value, default, *pairs):
    if len(pairs) % 2:
        raise ExpressionError('Switch requires value/result pairs after the default')
    for i in range(0, len(pairs), 2):
        if _compare('=', value, pairs[i]):
            return pairs[i + 1]
    return default


@dataclass(frozen=True)
class FunctionSpec:
    impl: Callable
    min_args: int
    max_args: int | None
    returns: str | Callable[[list[str]], str]  # kind or fn(arg_kinds)->kind
    lazy: bool = False  # receives thunks (IIF)


def _same_as(i):
    return lambda kinds: kinds[i] if i < len(kinds) else 'any'


def _unify_kinds(kinds):
    ks = {k for k in kinds if k not in ('null', 'any')}
    return ks.pop() if len(ks) == 1 else 'any'


FUNCTIONS: dict[str, FunctionSpec] = {
    # tests / conditional
    'ISNULL': FunctionSpec(lambda ctx, v: v is None, 1, 1, 'bool'),
    'ISEMPTY': FunctionSpec(lambda ctx, v: v is None or v == '', 1, 1, 'bool'),
    'ISNUMBER': FunctionSpec(_is_number_text, 1, 1, 'bool'),
    'ISSTRING': FunctionSpec(lambda ctx, v: isinstance(v, str), 1, 1, 'bool'),
    'IIF': FunctionSpec(_iif, 3, 3, lambda k: _unify_kinds(k[1:])),
    'COALESCE': FunctionSpec(_coalesce, 1, None, _unify_kinds),
    'SWITCH': FunctionSpec(_switch, 2, None, lambda k: _unify_kinds([k[1]] + k[3::2])),
    'NULL': FunctionSpec(lambda ctx: None, 0, 0, 'null'),
    # string
    'CONTAINS': FunctionSpec(_contains, 2, 3, 'bool'),
    'STARTSWITH': FunctionSpec(_starts, 2, 3, 'bool'),
    'ENDSWITH': FunctionSpec(_ends, 2, 3, 'bool'),
    'LENGTH': FunctionSpec(_str_fn_null_as_empty(len), 1, 1, 'number'),
    'UPPERCASE': FunctionSpec(_str_fn(str.upper), 1, 1, 'string'),
    'LOWERCASE': FunctionSpec(_str_fn(str.lower), 1, 1, 'string'),
    'TITLECASE': FunctionSpec(_title, 1, 1, 'string'),
    'TRIM': FunctionSpec(_trim('strip'), 1, 2, 'string'),
    'TRIMLEFT': FunctionSpec(_trim('lstrip'), 1, 2, 'string'),
    'TRIMRIGHT': FunctionSpec(_trim('rstrip'), 1, 2, 'string'),
    'LEFT': FunctionSpec(_null_guard(lambda ctx, s, n: _s(s)[:max(int(n), 0)]), 2, 2, 'string'),
    'RIGHT': FunctionSpec(_null_guard(lambda ctx, s, n: _s(s)[len(_s(s)) - max(int(n), 0):] if int(n) > 0 else ''), 2, 2, 'string'),
    'SUBSTRING': FunctionSpec(_substr, 2, 3, 'string'),
    'PADLEFT': FunctionSpec(_pad('L'), 2, 3, 'string'),
    'PADRIGHT': FunctionSpec(_pad('R'), 2, 3, 'string'),
    'REPLACE': FunctionSpec(_replace, 3, 3, 'string'),
    'REVERSESTRING': FunctionSpec(_str_fn(lambda s: s[::-1]), 1, 1, 'string'),
    'FINDSTRING': FunctionSpec(_findstring, 2, 2, 'number'),
    'CHARTOINT': FunctionSpec(_null_guard(lambda ctx, s: ord(_s(s)[0]) if _s(s) else 0), 1, 1, 'number'),
    'CHARFROMINT': FunctionSpec(_null_guard(lambda ctx, n: chr(int(n))), 1, 1, 'string'),
    'COUNTWORDS': FunctionSpec(_count_words, 1, 1, 'number'),
    'REGEX_MATCH': FunctionSpec(_rx_match, 2, 3, 'bool'),
    'REGEX_REPLACE': FunctionSpec(_rx_replace, 3, 4, 'string'),
    'REGEX_COUNTMATCHES': FunctionSpec(_rx_count, 2, 3, 'number'),
    # conversion
    'TONUMBER': FunctionSpec(_to_number, 1, 1, 'number'),
    'TOSTRING': FunctionSpec(_to_string, 1, 2, 'string'),
    'TODATE': FunctionSpec(_to_date, 1, 1, 'date'),
    'TODATETIME': FunctionSpec(_to_datetime, 1, 1, 'datetime'),
    # math
    'ABS': FunctionSpec(_num_fn(abs), 1, 1, 'number'),
    'CEIL': FunctionSpec(_num_fn(math.ceil), 1, 1, 'number'),
    'FLOOR': FunctionSpec(_num_fn(math.floor), 1, 1, 'number'),
    'ROUND': FunctionSpec(lambda ctx, x, m: _round(x, m), 2, 2, 'number'),
    'POW': FunctionSpec(_num_fn(lambda a, b: math.pow(a, b)), 2, 2, 'number'),
    'SQRT': FunctionSpec(_num_fn(math.sqrt), 1, 1, 'number'),
    'EXP': FunctionSpec(_num_fn(math.exp), 1, 1, 'number'),
    'LOG': FunctionSpec(_num_fn(math.log), 1, 1, 'number'),
    'LOG10': FunctionSpec(_num_fn(math.log10), 1, 1, 'number'),
    'MOD': FunctionSpec(lambda ctx, a, b: _arith('%', a, b, ctx), 2, 2, 'number'),
    'MIN': FunctionSpec(_minmax(min), 1, None, _unify_kinds),
    'MAX': FunctionSpec(_minmax(max), 1, None, _unify_kinds),
    'PI': FunctionSpec(lambda ctx: math.pi, 0, 0, 'number'),
    # date/time
    'DATETIMENOW': FunctionSpec(lambda ctx: ctx.now().replace(microsecond=0), 0, 0, 'datetime'),
    'DATETIMETODAY': FunctionSpec(lambda ctx: ctx.now().replace(hour=0, minute=0, second=0, microsecond=0), 0, 0, 'datetime'),
    'DATETIMEADD': FunctionSpec(lambda ctx, d, n, u: _dt_add(d, n, u), 3, 3, _same_as(0)),
    'DATETIMEDIFF': FunctionSpec(lambda ctx, a, b, u: _dt_diff(a, b, u), 3, 3, 'number'),
    'DATETIMEFORMAT': FunctionSpec(lambda ctx, d, f: _dt_format(d, f), 2, 2, 'string'),
    'DATETIMEPARSE': FunctionSpec(_dt_parse, 2, 2, 'any'),
    'DATETIMEYEAR': FunctionSpec(_date_part('year'), 1, 1, 'number'),
    'DATETIMEMONTH': FunctionSpec(_date_part('month'), 1, 1, 'number'),
    'DATETIMEDAY': FunctionSpec(_date_part('day'), 1, 1, 'number'),
    'DATETIMEHOUR': FunctionSpec(_date_part('hour'), 1, 1, 'number'),
    'DATETIMEMINUTE': FunctionSpec(_date_part('minute'), 1, 1, 'number'),
    'DATETIMESECOND': FunctionSpec(_date_part('second'), 1, 1, 'number'),
    'DATETIMEFIRSTOFMONTH': FunctionSpec(_first_of_month, 1, 1, 'datetime'),
    'DATETIMELASTOFMONTH': FunctionSpec(_last_of_month, 1, 1, 'datetime'),
}


# =========================================================================== compiler

@dataclass
class CompiledExpression:
    source: str
    ast: Node
    kind: str  # statically inferred result kind
    fields: frozenset  # referenced field names
    max_lookback: int
    max_lookahead: int
    _fn: Callable

    def evaluate(self, row: tuple, ctx: EvalContext):
        try:
            return self._fn(row, ctx)
        except ExpressionError:
            raise
        except (TypeError, ValueError, ArithmeticError) as e:
            raise ExpressionError(f'Evaluation failed: {e}') from None


def compile_expression(source: str, schema: Schema, *, allow_row_refs: bool = False,
                       extra_kinds: dict[str, str] | None = None) -> CompiledExpression:
    """Parse, type-check against ``schema`` and compile to a closure.

    ``extra_kinds`` lets callers declare additional names (e.g. a Generate Rows
    loop field, or a Formula field that will be created) that are resolved by
    the caller-supplied row (positioned after the schema fields).
    """
    ast = parse(source)
    names = schema.names + list((extra_kinds or {}).keys())
    kinds = {f.name: kind_of_field(f.type) for f in schema}
    kinds.update(extra_kinds or {})
    index = {n: i for i, n in enumerate(names)}
    refs: set[str] = set()
    look = [0, 0]

    def check(node: Node) -> str:
        if isinstance(node, Literal):
            v = node.value
            if v is None:
                return 'null'
            if isinstance(v, bool):
                return 'bool'
            if isinstance(v, (int, float)):
                return 'number'
            return 'string'
        if isinstance(node, FieldRef):
            if node.name not in kinds:
                raise ExpressionError(f'Unknown field [{node.name}]', position=node.pos)
            if node.row_offset and not allow_row_refs:
                raise ExpressionError('Row references ([Row-n:Field]) are only allowed in multi-row expressions',
                                      position=node.pos)
            refs.add(node.name)
            if node.row_offset < 0:
                look[0] = max(look[0], -node.row_offset)
            elif node.row_offset > 0:
                look[1] = max(look[1], node.row_offset)
            return kinds[node.name]
        if isinstance(node, Unary):
            k = check(node.operand)
            if node.op == 'NEG' and k not in ('number', 'null', 'any'):
                raise ExpressionError(f'Unary minus requires a number, got {k}', position=node.pos)
            return 'bool' if node.op == 'NOT' else k
        if isinstance(node, Binary):
            a, b = check(node.left), check(node.right)
            if node.op in ('AND', 'OR'):
                return 'bool'
            if node.op in ('=', '!=', '<', '<=', '>', '>='):
                if not _compatible(a, b) and not ({a, b} <= {'number', 'bool'}):
                    raise ExpressionError(f'Cannot compare {a} with {b}', position=node.pos)
                return 'bool'
            if node.op == '+' and 'string' in (a, b):
                if {a, b} - {'string', 'null', 'any'}:
                    raise ExpressionError(f"'+' cannot combine {a} and {b}; use ToString()", position=node.pos)
                return 'string'
            for k in (a, b):
                if k not in ('number', 'bool', 'null', 'any'):
                    raise ExpressionError(f'Operator {node.op!r} requires numbers, got {k}', position=node.pos)
            return 'number'
        if isinstance(node, InList):
            check(node.operand)
            for item in node.items:
                check(item)
            return 'bool'
        if isinstance(node, Call):
            spec = FUNCTIONS.get(node.name)
            if not spec:
                raise ExpressionError(f'Unknown function {node.name}', position=node.pos)
            n = len(node.args)
            if n < spec.min_args or (spec.max_args is not None and n > spec.max_args):
                bound = spec.min_args if spec.max_args == spec.min_args else f'{spec.min_args}..{spec.max_args or "n"}'
                raise ExpressionError(f'{node.name} expects {bound} arguments, got {n}', position=node.pos)
            arg_kinds = [check(a) for a in node.args]
            return spec.returns(arg_kinds) if callable(spec.returns) else spec.returns
        if isinstance(node, Conditional):
            outs = []
            for c, v in node.branches:
                check(c)
                outs.append(check(v))
            outs.append(check(node.otherwise))
            return _unify_kinds(outs)
        raise ExpressionError(f'Unsupported node {node!r}')  # pragma: no cover

    def build(node: Node) -> Callable:
        if isinstance(node, Literal):
            v = node.value
            return lambda row, ctx: v
        if isinstance(node, FieldRef):
            i = index[node.name]
            if node.row_offset:
                off = node.row_offset
                return lambda row, ctx: ctx.row_accessor(off, i)
            return lambda row, ctx: row[i]
        if isinstance(node, Unary):
            f = build(node.operand)
            if node.op == 'NOT':
                return lambda row, ctx: not truthy(f(row, ctx))

            def neg(row, ctx):
                v = f(row, ctx)
                return None if v is None else -v
            return neg
        if isinstance(node, Binary):
            l, r, op = build(node.left), build(node.right), node.op
            if op == 'AND':
                return lambda row, ctx: truthy(l(row, ctx)) and truthy(r(row, ctx))
            if op == 'OR':
                return lambda row, ctx: truthy(l(row, ctx)) or truthy(r(row, ctx))
            if op in ('=', '!=', '<', '<=', '>', '>='):
                return lambda row, ctx: _compare(op, l(row, ctx), r(row, ctx))
            return lambda row, ctx: _arith(op, l(row, ctx), r(row, ctx), ctx)
        if isinstance(node, InList):
            f = build(node.operand)
            items = [build(i) for i in node.items]
            neg = node.negated

            def inlist(row, ctx):
                v = f(row, ctx)
                found = any(_compare('=', v, it(row, ctx)) for it in items)
                return (not found) if neg else found
            return inlist
        if isinstance(node, Call):
            spec = FUNCTIONS[node.name]
            args = [build(a) for a in node.args]
            impl = spec.impl
            if node.name == 'IIF':  # lazy: evaluate only the chosen branch
                c, a, b = args
                return lambda row, ctx: a(row, ctx) if truthy(c(row, ctx)) else b(row, ctx)
            return lambda row, ctx: impl(ctx, *[a(row, ctx) for a in args])
        if isinstance(node, Conditional):
            branches = [(build(c), build(v)) for c, v in node.branches]
            other = build(node.otherwise)

            def cond(row, ctx):
                for c, v in branches:
                    if truthy(c(row, ctx)):
                        return v(row, ctx)
                return other(row, ctx)
            return cond
        raise ExpressionError(f'Unsupported node {node!r}')  # pragma: no cover

    kind = check(ast)
    return CompiledExpression(source, ast, kind, frozenset(refs), look[0], look[1], build(ast))


def field_type_for_kind(kind: str, values=None) -> FieldType:
    """Default output type for a formula result kind (engine policy)."""
    if kind == 'number':
        if values is not None:
            nonnull = [v for v in values if v is not None]
            if nonnull and all(isinstance(v, int) and not isinstance(v, bool) for v in nonnull):
                return FieldType.INT64
        return FieldType.DOUBLE
    return {'bool': FieldType.BOOL, 'string': FieldType.V_WSTRING, 'date': FieldType.DATE,
            'time': FieldType.TIME, 'datetime': FieldType.DATETIME, 'blob': FieldType.BLOB}.get(kind, FieldType.V_WSTRING)
