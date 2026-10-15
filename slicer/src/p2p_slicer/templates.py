"""Bambu Studio's G-code template language, the variables the X2D templates read, and the text
Bambu wraps around each rendered template. Written from Bambu's sliced output (20 calibration
cases), not from its source.

Use:

    base = base_env(cfg, used_slots=[1, 2, 5], first_layer_slots=[2])   # once per print
    text = start_block(start_env(base, initial_filament_id=1, ...))
    text, feed = change_block(change_env(base, current_filament_id=1, next_filament_id=4, ...),
                              feedrate=5400)
    text = layer_change_block(layer_change_env(base, layer_num=3, ...))

`render(template, env)` is the bare language. Checked by tests/test_slicer_templates.py: every
start, change, filament start/end, layer change, timelapse and end block in the 20 truth cases
(3,163 changes, 2,551 layer changes, 2,551 timelapses) comes out identical to Bambu's, blank
lines included, once Bambu's post-placement inserts are dropped (strip_inserted).

Language (every construct the X2D templates use): `[name]`, `[name[index]]`; `{expr}` with int,
float and string literals, `true`/`false`, variables, `name[expr]`, `+ - * / %` (int / int
truncates: `new_filament_e_feedrate/2` prints 149 for 299), `== != < > <= >=`, `&& || !`,
`and or not`, parentheses, `c ? a : b`, and min, max, ceil, floor, int, round, abs, is_nil;
`{if}` / `{elsif}` / `{else}` / `{endif}`, nested, conditions spanning lines. Tags vanish and the
text around them stays, newlines included. Numbers: ints as ints, floats with 6 significant
digits (`%g`: 299.339, 3.2, 102, 0). The X2D templates use no comments or escapes.

Where the per-event values come from (what in the slice decides them). Filament ids are 0-based
(slot - 1), nozzles 0 left / 1 right (filament_map - 1), physical extruder ids through
physical_extruder_map (X2D: left = 1, right = 0).

Print-wide (base_env, start_env):
- used_slots / first_layer_slots: filaments the plate prints, and those on layer 1 (the bed
  temperature comes from the layer 1 set; SOURCE: nameplate_5c_020 uses PETG above layer 1 and
  heats the bed to 55, the swatch card has PETG on layer 1 and heats to 70).
- initial_filament_id / initial_no_support_filament_id: the first filament printed on layer 1
  (the start G-code loads it). initial_nozzle_id: its nozzle (ASSUMED logical nozzle; both X2D
  nozzles are 0.4 so the data cannot tell). initial_no_support_hotend: -1 (SOURCE, all cases).
- first_layer_print_min / first_layer_print_size: [x, y] of the layer 1 bounding box, prime tower
  included (G29 `X Y I J`), from the planner.
- max_print_z: top of the print (only tested against 145 mm; ASSUMED the highest layer z).
- first_filaments / first_non_support_filaments: per physical extruder, the first filament that
  extruder prints, -1 if none (SOURCE: `M620.17 T0 .. L4` / `T1 .. L1` on keychain_4c_020).
- hold_chamber_temp_for_flat_print: False in every truth case; what sets it is not known.
- most_used_physical_extruder_id: most_used_physical_extruder() (layers with model on each
  extruder; SOURCE: all 19 cases with a timelapse `E`).

Per change (change_env), in print order:
- current_filament_id, next_filament_id: the loaded filament and the one the change loads.
- toolchange_count: 1-based index of the change in the print (SOURCE: the travel-path comment
  prints on the second change, the spiral lift from the second on).
- layer_num (0-based), layer_z (the layer's print z), max_layer_z (highest z so far).
- flush_length: mm of the new filament purged to the chute (the change planner's number; 0 for
  a nozzle switch that needs no swap).
- z_after_toolchange: z of the nozzle when the change starts: the z of the last move before the
  block, which is the layer z plus the retract lift (SOURCE: all spiral lift lines).
- old/new_filament_temp: layer_temp() of each filament; old/new_filament_e_feedrate:
  e_feedrate(); filament_cooling_before_tower: 0 on layer 1 and on a prime tower interface
  change, else config (SOURCE: `M620.15`).
- is_prime_tower_interface: the tower planner prints an interface for this change (seen once:
  first PETG change above PLA on nameplate_5c_020), with the new filament's
  filament_tower_interface_purge_volume / _print_temp.
- current_hotend / next_hotend: -1; travel_point_1..3_x/y: (54, 0), (54, 0), (54, 245) (SOURCE:
  identical in all 19 multi-color cases; only printed in comments on the second change);
  x/y_after_toolchange: only in a branch no X2D change reaches (next_filament_id >= 255).
- current_nozzle_id / next_nozzle_id: logical nozzles (ASSUMED, as initial_nozzle_id).
- long_retraction_when_cut / retraction_distance_when_cut: long_retractions_when_cut of the
  current nozzle, filament_retraction_distances_when_cut unless nil else
  retraction_distances_when_cut (ASSUMED; off in every truth case).
- long_retraction_when_ec / retraction_distance_when_ec: long_retractions_when_ec and
  retraction_distances_when_ec of the current filament (SOURCE: `M620.11 K1 .. R3` / `R4`).
- initial_layer_acceleration / travel_acceleration: the next nozzle's value (ASSUMED which
  nozzle; both are equal on the X2D).

Per layer change (layer_change_env): layer_num of the layer starting, layer_z, max_layer_z,
current_filament_id loaded when the layer starts. Per timelapse (timelapse_env): layer_z,
max_layer_z, curr_physical_extruder_id of the loaded filament, most_used_physical_extruder_id,
and the timelapse planner's choice: timelapse_inline_photo (photo taken inside the layer's
last outer wall, `M971`), else has_timelapse_safe_pos with timelapse_pos_x/y (`M9711 .. U V`),
else neither (the template then lifts the nozzle when curr differs from most used). End
(end_env): max_layer_z of the last layer. Filament start/end: the filament id (the X2D
templates are plain comments).

Wrapper lines (what Bambu adds around the rendered text, reproduced by the *_block and place_*
functions):
- Start: `; FEATURE: Custom`, start text, `; MACHINE_START_GCODE_END`, the initial filament's
  filament_start_gcode. Before: `M73 P0 R..`, machine limits (`M201`, `M203`, `M204`, `M205`),
  `M106 S0`, `M106 P2 S0`. After: `;VT<n> H-1`, `G90`, `G21`, `M83 ; use relative distances
  for extrusion`, `M981 S1 P20000 ;open spaghetti detector`, `M204 S..`, `G1 Z.. F..`.
- Change: old filament's filament_end_gcode, a newline, change text. Bambu drops repeated feed
  rates in the change text (`G1 Z3.2 F3000` twice prints the second as `G1 Z3.2`) and removes
  its last line `;not set fan changing filament`. Before: the tower block header, on a nozzle
  switch the ramming block (`; NOZZLE_CHANGE_START OF.. NF.. ON.. NN..`), wipe, retract, lift.
  After: `M106 S..` (part fan), travel to the tower, a blank line, the new filament's
  filament_start_gcode.
- Layer change: after `; CHANGE_LAYER`, `; Z_HEIGHT: z`, `; LAYER_HEIGHT: h`, the retract;
  followed by one extra newline, then Bambu's `M106 S..`.
- Timelapse: the text plus one extra newline; before it the retract and lift, or it sits inside
  the layer's last outer wall for an inline photo.
- End: `; FEATURE: Custom`, `; MACHINE_END_GCODE_START`, the last filament's
  filament_end_gcode, end text. Before: `M106 S0`, `M106 P2 S0`, `M981 S0 P20000 ; close
  spaghetti detector`. After: `M73 P100 R0`, `; EXECUTABLE_BLOCK_END`.
- Inserted later, anywhere, by time (not template output; strip_inserted drops them):
  `M73 P.. R..`, `M73 E..`, `M400` + `M104 T.. S.. N0 ;Multi extruder pre cooling`,
  `M632 S.. W` + `M104 .. ;Multi extruder pre heating` + `M633`, `M104 .. ;Wipe tower reheat
  before wipe`.
- before_layer_change_gcode, printing_by_object_gcode, template_custom_gcode and
  wrapping_detection_gcode are empty for the X2D and add nothing.
"""

from __future__ import annotations

import math
import operator
import re
from collections import ChainMap
from collections.abc import Callable, Mapping, Sequence
from functools import lru_cache
from typing import Any


class TemplateError(ValueError):
    """A template that does not parse, or an expression that cannot be evaluated."""


# ----------------------------------------------------------------------------- values


class Nil:
    """A config value written as `nil` (a filament override that is not set). Use NIL."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "nil"


NIL = Nil()


class Enum:
    """An enum config value: compares as its name against a string and as its number against a
    number (the X2D templates do both: `curr_bed_type == "Textured PEI Plate"`,
    `z_hop_types[i] == 3`, `timelapse_type == 1`)."""

    __slots__ = ("name", "value")

    def __init__(self, name: str, value: int) -> None:
        self.name = name
        self.value = value

    def __repr__(self) -> str:
        return f"Enum({self.name!r}, {self.value})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Enum) and other.name == self.name and other.value == self.value

    def __hash__(self) -> int:
        return hash((self.name, self.value))


def format_value(v: Any) -> str:
    """Text of a value as the template language prints it.

    Integers print as integers. Floats print with 6 significant digits and no trailing zeros
    (C `%g`): 12/2.4053*60 prints 299.339, 0.2 + 3.0 prints 3.2, 102.0 prints 102, 0.0 prints 0.
    SOURCE: every number in the 20 calibration G-code files fits this rule."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        s = f"{v:g}"
        return "0" if s == "-0" else s
    if isinstance(v, Enum):
        return v.name
    if isinstance(v, str):
        return v
    if v is NIL:
        return "nil"
    if isinstance(v, (list, tuple)):
        raise TemplateError("a vector variable needs an index")
    raise TemplateError(f"cannot print {v!r}")


# ----------------------------------------------------------------------------- arithmetic


def _num(v: Any) -> int | float:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, Enum):
        return v.value
    raise TemplateError(f"not a number: {v!r}")


def _is_int(v: Any) -> bool:
    return isinstance(v, (int, Enum)) and not isinstance(v, float)


def _add(a: Any, b: Any) -> Any:
    if isinstance(a, str) and isinstance(b, str):
        return a + b
    return _num(a) + _num(b)


def _sub(a: Any, b: Any) -> Any:
    return _num(a) - _num(b)


def _mul(a: Any, b: Any) -> Any:
    return _num(a) * _num(b)


def _div(a: Any, b: Any) -> Any:
    """Integer division truncates toward zero when both sides are integers (as in C). SOURCE:
    `{max(new_filament_e_feedrate/2, 100)}` prints 149 for a feed rate of 299."""
    x, y = _num(a), _num(b)
    if _is_int(a) and _is_int(b):
        if y == 0:
            raise TemplateError("integer division by zero")
        q = abs(x) // abs(y)
        return q if (x >= 0) == (y >= 0) else -q
    if y == 0:
        raise TemplateError("division by zero")
    return x / y


def _mod(a: Any, b: Any) -> Any:
    x, y = _num(a), _num(b)
    if y == 0:
        raise TemplateError("modulo by zero")
    if _is_int(a) and _is_int(b):
        return int(math.fmod(x, y))
    return math.fmod(x, y)


def _cmp_operands(a: Any, b: Any) -> tuple[Any, Any]:
    """Enums compare by name against strings and by number against numbers."""
    if isinstance(a, Enum):
        a = a.name if isinstance(b, (str, Enum)) else a.value
    if isinstance(b, Enum):
        b = b.name if isinstance(a, str) else b.value
    if isinstance(a, str) != isinstance(b, str):
        raise TemplateError(f"cannot compare {a!r} with {b!r}")
    if not isinstance(a, str):
        a, b = _num(a), _num(b)
    return a, b


def _eq(a: Any, b: Any) -> bool:
    if a is NIL or b is NIL:
        return a is b
    x, y = _cmp_operands(a, b)
    return x == y


def _truth(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, Enum):
        return v.value != 0
    if isinstance(v, str):
        return v not in ("", "0", "false")
    if v is NIL:
        return False
    raise TemplateError(f"not a condition: {v!r}")


def _round_half_away(x: float) -> int:
    return math.floor(abs(x) + 0.5) * (1 if x >= 0 else -1)


def _fn_min(a: Any, b: Any) -> Any:
    r = min(_num(a), _num(b))
    return r if _is_int(a) and _is_int(b) else float(r)


def _fn_max(a: Any, b: Any) -> Any:
    r = max(_num(a), _num(b))
    return r if _is_int(a) and _is_int(b) else float(r)


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "min": _fn_min,
    "max": _fn_max,
    "ceil": lambda a: math.ceil(_num(a)),
    "floor": lambda a: math.floor(_num(a)),
    "int": lambda a: int(_num(a)),
    "round": lambda a: _round_half_away(_num(a)),
    "abs": lambda a: abs(_num(a)),
    "is_nil": lambda a: a is NIL,
}


# ----------------------------------------------------------------------------- expressions

_TOKEN = re.compile(r"""
    \s*(?:
      (?P<num>(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?|\d+[eE][-+]?\d+|\d+)
    | (?P<str>"(?:[^"\\]|\\.)*")
    | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<op>\|\||&&|==|!=|<>|<=|>=|[-+*/%<>!?:()\[\],])
    )""", re.VERBOSE)

_BINARY: dict[str, tuple[int, Callable[[Any, Any], Any]]] = {
    "==": (4, _eq),
    "!=": (4, lambda a, b: not _eq(a, b)),
    "<>": (4, lambda a, b: not _eq(a, b)),
    "<": (5, lambda a, b: operator.lt(*_cmp_operands(a, b))),
    ">": (5, lambda a, b: operator.gt(*_cmp_operands(a, b))),
    "<=": (5, lambda a, b: operator.le(*_cmp_operands(a, b))),
    ">=": (5, lambda a, b: operator.ge(*_cmp_operands(a, b))),
    "+": (6, _add),
    "-": (6, _sub),
    "*": (7, _mul),
    "/": (7, _div),
    "%": (7, _mod),
}
_WORD_OPS = {"and": "&&", "or": "||", "not": "!"}

Env = Mapping[str, Any]
Node = Callable[[Env], Any]


def _tokenize(src: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    n = len(src)
    while pos < n:
        if src[pos:].strip() == "":
            break
        m = _TOKEN.match(src, pos)
        if not m or m.end() == pos:
            raise TemplateError(f"bad token at {src[pos:pos + 20]!r} in {src!r}")
        pos = m.end()
        kind = m.lastgroup or ""
        text = m.group(kind)
        if kind == "id" and text in _WORD_OPS:
            kind, text = "op", _WORD_OPS[text]
        out.append((kind, text))
    out.append(("end", ""))
    return out


def _const(v: Any) -> Node:
    return lambda env: v


def _var(name: str) -> Node:
    return lambda env: _lookup(env, name)


def _ternary(c: Node, a: Node, b: Node) -> Node:
    return lambda env: a(env) if _truth(c(env)) else b(env)


def _or(x: Node, y: Node) -> Node:
    return lambda env: _truth(x(env)) or _truth(y(env))


def _and(x: Node, y: Node) -> Node:
    return lambda env: _truth(x(env)) and _truth(y(env))


def _binary(f: Callable[[Any, Any], Any], x: Node, y: Node) -> Node:
    return lambda env: f(x(env), y(env))


def _call(f: Callable[..., Any], args: list[Node]) -> Node:
    return lambda env: f(*[a(env) for a in args])


def _indexed(v: Node, i: Node, name: str) -> Node:
    return lambda env: _index(v(env), i(env), name)


def _lookup(env: Env, name: str) -> Any:
    try:
        return env[name]
    except KeyError:
        raise TemplateError(f"unknown variable {name!r}") from None


def _index(v: Any, i: Any, name: str) -> Any:
    if not isinstance(v, (list, tuple)):
        raise TemplateError(f"{name} is not a vector")
    k = int(_num(i))
    if not 0 <= k < len(v):
        raise TemplateError(f"{name}[{k}] out of range (size {len(v)})")
    return v[k]


class _Parser:
    """Pratt parser from tokens to a closure env -> value. Precedence, low to high: ?:, ||, &&,
    == !=, < > <= >=, + -, * / %, unary ! - +, indexing."""

    def __init__(self, src: str) -> None:
        self.src = src
        self.toks = _tokenize(src)
        self.i = 0

    def peek(self) -> tuple[str, str]:
        return self.toks[self.i]

    def take(self) -> tuple[str, str]:
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, text: str) -> None:
        k, t = self.take()
        if t != text or k not in ("op",):
            raise TemplateError(f"expected {text!r}, got {t!r} in {self.src!r}")

    def parse(self) -> Node:
        node = self.expr(0)
        if self.peek()[0] != "end":
            raise TemplateError(f"unexpected {self.peek()[1]!r} in {self.src!r}")
        return node

    def expr(self, min_bp: int) -> Node:
        left = self.unary()
        while True:
            kind, t = self.peek()
            if kind != "op":
                break
            if t == "?" and min_bp <= 1:
                self.take()
                a = self.expr(1)
                self.expect(":")
                b = self.expr(1)
                left = _ternary(left, a, b)
                continue
            if t == "||" and min_bp <= 2:
                self.take()
                r = self.expr(3)
                left = _or(left, r)
                continue
            if t == "&&" and min_bp <= 3:
                self.take()
                r = self.expr(4)
                left = _and(left, r)
                continue
            if t in _BINARY:
                bp, fn = _BINARY[t]
                if bp < min_bp:
                    break
                self.take()
                r = self.expr(bp + 1)
                left = _binary(fn, left, r)
                continue
            break
        return left

    def unary(self) -> Node:
        kind, t = self.peek()
        if kind == "op" and t in ("!", "-", "+"):
            self.take()
            x = self.unary()
            if t == "!":
                return lambda env: not _truth(x(env))
            if t == "-":
                return lambda env: -_num(x(env))
            return lambda env: _num(x(env))
        return self.primary()

    def primary(self) -> Node:
        kind, t = self.take()
        if kind == "num":
            return _const(int(t) if t.isdigit() else float(t))
        if kind == "str":
            return _const(re.sub(r"\\(.)", r"\1", t[1:-1]))
        if kind == "id":
            if t in ("true", "false"):
                return _const(t == "true")
            if self.peek() == ("op", "("):
                self.take()
                args: list[Node] = []
                if self.peek() != ("op", ")"):
                    while True:
                        args.append(self.expr(0))
                        if self.peek() != ("op", ","):
                            break
                        self.take()
                self.expect(")")
                fn = FUNCTIONS.get(t)
                if fn is None:
                    raise TemplateError(f"unknown function {t!r}")
                return _call(fn, args)
            node = _var(t)
            while self.peek() == ("op", "["):
                self.take()
                idx = self.expr(0)
                self.expect("]")
                node = _indexed(node, idx, t)
            return node
        if kind == "op" and t == "(":
            node = self.expr(0)
            self.expect(")")
            return node
        raise TemplateError(f"unexpected {t!r} in {self.src!r}")


@lru_cache(maxsize=4096)
def compile_expr(src: str) -> Node:
    """Compile one expression (the inside of `{...}`) to a function of the variables."""
    return _Parser(src).parse()


def evaluate(src: str, env: Env) -> Any:
    return compile_expr(src)(env)


# ----------------------------------------------------------------------------- templates

_SUBST = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*)(?:\[([A-Za-z_][A-Za-z0-9_]*|\d+)\])?\]")
_CONTROL = re.compile(r"\s*(if|elsif|else|endif)\b(.*)\Z", re.DOTALL)

# A compiled template is a list of parts. A part is a str (literal text), a Node (value to
# print) or an _If.


class _If:
    __slots__ = ("branches", "otherwise")

    def __init__(self) -> None:
        self.branches: list[tuple[Node, list[Any]]] = []
        self.otherwise: list[Any] = []


def _subst_node(name: str, idx: str | None) -> Node:
    if idx is None:
        return lambda env: _lookup(env, name)
    if idx.isdigit():
        k = int(idx)
        return lambda env: _index(_lookup(env, name), k, name)
    return lambda env: _index(_lookup(env, name), _lookup(env, idx), name)


def _close_brace(text: str, i: int) -> int:
    """Index of the `}` closing the `{` at i (skips strings)."""
    j = i + 1
    n = len(text)
    while j < n:
        c = text[j]
        if c == '"':
            j += 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
        elif c == "}":
            return j
        elif c == "{":
            raise TemplateError(f"nested '{{' at {text[i:j + 20]!r}")
        j += 1
    raise TemplateError(f"unclosed '{{' at {text[i:i + 40]!r}")


def _literal_parts(text: str) -> list[Any]:
    """Literal text with legacy `[name]` / `[name[index]]` substitutions."""
    parts: list[Any] = []
    pos = 0
    for m in _SUBST.finditer(text):
        if m.start() > pos:
            parts.append(text[pos:m.start()])
        parts.append(_subst_node(m.group(1), m.group(2)))
        pos = m.end()
    if pos < len(text):
        parts.append(text[pos:])
    return parts


@lru_cache(maxsize=256)
def compile_template(text: str) -> tuple[Any, ...]:
    """Parse a template into parts (cached by text)."""
    root: list[Any] = []
    stack: list[tuple[_If, list[Any]]] = []   # open ifs and the part list they sit in
    cur = root
    pos = 0
    n = len(text)
    while pos < n:
        i = text.find("{", pos)
        if i < 0:
            cur.extend(_literal_parts(text[pos:]))
            break
        if i > pos:
            cur.extend(_literal_parts(text[pos:i]))
        j = _close_brace(text, i)
        body = text[i + 1:j]
        pos = j + 1
        m = _CONTROL.match(body)
        if m:
            word, rest = m.group(1), m.group(2).strip()
            if word == "if":
                node = _If()
                node.branches.append((compile_expr(rest), []))
                cur.append(node)
                stack.append((node, cur))
                cur = node.branches[-1][1]
            elif word == "elsif":
                if not stack:
                    raise TemplateError("{elsif} without {if}")
                node = stack[-1][0]
                node.branches.append((compile_expr(rest), []))
                cur = node.branches[-1][1]
            elif word == "else":
                if not stack or rest:
                    raise TemplateError("bad {else}")
                cur = stack[-1][0].otherwise
            else:
                if not stack or rest:
                    raise TemplateError("bad {endif}")
                cur = stack.pop()[1]
            continue
        cur.append(compile_expr(body))
    if stack:
        raise TemplateError("{if} without {endif}")
    return tuple(root)


def _emit(parts: Sequence[Any], env: Env, out: list[str]) -> None:
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, _If):
            for cond, body in p.branches:
                if _truth(cond(env)):
                    _emit(body, env, out)
                    break
            else:
                _emit(p.otherwise, env, out)
        else:
            out.append(format_value(p(env)))


def render(text: str, env: Env) -> str:
    """Render a Bambu Studio G-code template.

    Supports `[name]` and `[name[index]]` substitution; `{expr}` with numbers, strings, `true`,
    `false`, variables, `name[expr]`, `+ - * / %`, `== != <> < > <= >=`, `&& || !` (and `and or
    not`), parentheses, `c ? a : b` and the functions in FUNCTIONS; `{if}`, `{elsif}`, `{else}`,
    `{endif}` blocks, nested and spanning lines. Control tags vanish and leave the text around
    them as it is, newlines included (Bambu does the same: a false `{if}` block leaves the
    newline after its `{endif}`)."""
    out: list[str] = []
    _emit(compile_template(text), env, out)
    return "".join(out)


# ----------------------------------------------------------------------------- typed config

# Integer-typed config keys among those the X2D templates read (and their close relatives).
# Integer typing matters for `/` and `%`. Other numeric keys are floats. ASSUMED for keys the
# truth data cannot tell apart (a float 40.0 and an int 40 print the same).
INT_KEYS = frozenset({
    "nozzle_temperature", "nozzle_temperature_initial_layer", "nozzle_temperature_range_low",
    "nozzle_temperature_range_high", "filament_flush_temp", "filament_flush_temp_fast",
    "filament_map", "filament_nozzle_map", "filament_map_2", "filament_self_index",
    "physical_extruder_map", "first_x_layer_fan_speed", "additional_cooling_fan_speed",
    "close_additional_fan_first_x_layers", "additional_fan_full_speed_layer", "fan_max_speed",
    "fan_min_speed", "close_fan_the_first_x_layers", "full_fan_speed_layer",
    "overhang_fan_speed", "temperature_vitrification", "chamber_temperatures",
    "cool_plate_temp", "cool_plate_temp_initial_layer", "eng_plate_temp",
    "eng_plate_temp_initial_layer", "hot_plate_temp", "hot_plate_temp_initial_layer",
    "textured_plate_temp", "textured_plate_temp_initial_layer", "supertack_plate_temp",
    "supertack_plate_temp_initial_layer", "timelapse_type", "long_retractions_when_cut",
    "long_retractions_when_ec", "enable_long_retraction_when_cut", "spiral_mode",
    "farthest_point_timelapse", "filament_is_support", "master_extruder_id",
    "print_extruder_id", "printer_extruder_id", "wall_loops", "top_shell_layers",
    "bottom_shell_layers", "raft_layers", "support_filament", "support_interface_filament",
    "wall_filament", "sparse_infill_filament", "solid_infill_filament",
})
STRING_KEYS = frozenset({"filament_type", "filament_vendor", "filament_ids", "curr_bed_type",
                         "filament_settings_id", "filament_colour", "default_filament_colour"})
# ASSUMED order of Bambu's z-hop enum. Only "Auto Lift" and "Spiral Lift" occur in the X2D
# profiles, and the change template treats both (0 and 3) the same way, so the truth data
# cannot tell the numbers apart.
Z_HOP_TYPES = {"Auto Lift": 0, "Normal Lift": 1, "Slope Lift": 2, "Spiral Lift": 3}
BED_TEMP_KEYS = {
    "Cool Plate": "cool_plate_temp",
    "Engineering Plate": "eng_plate_temp",
    "High Temp Plate": "hot_plate_temp",
    "Textured PEI Plate": "textured_plate_temp",
    "Supertack Plate": "supertack_plate_temp",
}
# 1.75 mm filament cross-section the X2D templates hard-code (mm2).
TEMPLATE_FILAMENT_AREA = 2.4053

_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\Z")


def typed_value(key: str, s: Any) -> Any:
    """One config string as the template language sees it."""
    if not isinstance(s, str):
        return s
    t = s.strip()
    if t == "nil":
        return NIL
    if key in ("z_hop_types", "filament_z_hop_types") and t in Z_HOP_TYPES:
        return Enum(t, Z_HOP_TYPES[t])
    if key in STRING_KEYS or not _NUM_RE.match(t):
        return s
    if key in INT_KEYS:
        try:
            return int(t)
        except ValueError:
            return int(float(t))
    return float(t)


def _resolved(cfg: Any) -> Mapping[str, Any]:
    return cfg.resolved if hasattr(cfg, "resolved") else cfg


def config_env(cfg: Any) -> dict[str, Any]:
    """Every resolved config key, typed: lists stay lists (per nozzle, per filament or machine
    limits as `config.resolve_config` returns them), scalars become scalars."""
    out: dict[str, Any] = {}
    for k, v in _resolved(cfg).items():
        if isinstance(v, list):
            out[k] = [typed_value(k, x) for x in v]
        else:
            out[k] = typed_value(k, v)
    return out


# ----------------------------------------------------------------------------- variable builders


def _fil_list(env: Mapping[str, Any], key: str, n: int, default: Any = 0) -> list[Any]:
    v = env.get(key)
    if not isinstance(v, list):
        return [v if v is not None else default] * n
    return [v[min(i, len(v) - 1)] if v else default for i in range(n)]


def base_env(cfg: Any, *, used_slots: Sequence[int] | None = None,
             first_layer_slots: Sequence[int] | None = None) -> dict[str, Any]:
    """Config variables plus the print-wide values Bambu derives before the start G-code.

    `cfg`: a `config.SliceConfig` (or its `resolved` dict). `used_slots`: 1-based slots the
    plate prints with (default: all). `first_layer_slots`: slots printed on the first layer
    (default: used_slots). Derived values:
    - flush_temperatures[i]: filament_flush_temp, or nozzle_temperature_range_high when 0.
    - flush_volumetric_speeds[i]: filament_flush_volumetric_speed, or
      filament_max_volumetric_speed when 0. (SOURCE for both fallbacks: `T240` / `T270` and
      `F299.339` (12 mm3/s) / `F523.843` (21 mm3/s) in every change block.)
    - nozzle_diameter_at_nozzle_id: nozzle_diameter (per nozzle).
    - z_hop_types: per filament (filament_z_hop_types unless nil, else the machine value of the
      filament's nozzle) because the change template indexes it by filament id. ASSUMED.
    - first_layer_temperature: nozzle_temperature_initial_layer (only read in the
      hold_chamber_temp_for_flat_print branch). ASSUMED.
    - bed_temperature_initial_layer_single: highest `<plate>_temp_initial_layer` for
      curr_bed_type among the first layer's filaments (bed_temperature_formula by_highest_temp;
      the first of them otherwise, ASSUMED). SOURCE: 70 on the swatch card (PETG on layer 1),
      55 on nameplate_5c_020 (PETG used, but not on layer 1) and on every PLA-only case.
    - overall_chamber_temperature: highest chamber_temperatures of the used filaments.
    - min_vitrification_temperature: lowest temperature_vitrification of the used filaments.
    - max_additional_fan: highest additional_cooling_fan_speed of the used filaments (`U0.7`).
    - is_all_bbl_filament: every used filament's vendor is "Bambu Lab". ASSUMED (no truth case
      reaches the branch that reads it).
    """
    env = config_env(cfg)
    n = len(env.get("filament_type") or env.get("filament_colour") or []) or 1
    slots = sorted(set(used_slots)) if used_slots else list(range(1, n + 1))
    ids = [s - 1 for s in slots if 0 < s <= n] or [0]
    first_ids = [s - 1 for s in sorted(set(first_layer_slots)) if 0 < s <= n] \
        if first_layer_slots else ids
    fmap = _fil_list(env, "filament_map", n, 1)

    ftemp = _fil_list(env, "filament_flush_temp", n, 0)
    thigh = _fil_list(env, "nozzle_temperature_range_high", n, 0)
    env["flush_temperatures"] = [t if _num(t) > 0 else h for t, h in zip(ftemp, thigh)]
    fspeed = _fil_list(env, "filament_flush_volumetric_speed", n, 0.0)
    vmax = _fil_list(env, "filament_max_volumetric_speed", n, 0.0)
    env["flush_volumetric_speeds"] = [s if _num(s) > 0 else m for s, m in zip(fspeed, vmax)]
    env["nozzle_diameter_at_nozzle_id"] = list(env.get("nozzle_diameter") or [0.4, 0.4])

    zh = env.get("z_hop_types")
    zh_n = zh if isinstance(zh, list) and zh else [Enum("Auto Lift", 0)]
    fz = _fil_list(env, "filament_z_hop_types", n, NIL)
    env["z_hop_types"] = [f if isinstance(f, Enum) else zh_n[min(int(fmap[i]) - 1, len(zh_n) - 1)]
                          for i, f in enumerate(fz)]
    env.setdefault("first_layer_temperature", env.get("nozzle_temperature_initial_layer"))

    bed = env.get("curr_bed_type")
    bed_name = bed.name if isinstance(bed, Enum) else str(bed or "Textured PEI Plate")
    bkey = BED_TEMP_KEYS.get(bed_name, "textured_plate_temp") + "_initial_layer"
    btemps = [_fil_list(env, bkey, n, 0)[i] for i in first_ids]
    if str(env.get("bed_temperature_formula", "by_highest_temp")) == "by_highest_temp":
        env["bed_temperature_initial_layer_single"] = max(btemps)
    else:
        env["bed_temperature_initial_layer_single"] = btemps[0]
    env["overall_chamber_temperature"] = max(_fil_list(env, "chamber_temperatures", n, 0)[i]
                                             for i in ids)
    env["min_vitrification_temperature"] = min(
        _fil_list(env, "temperature_vitrification", n, 0)[i] for i in ids)
    env["max_additional_fan"] = max(_fil_list(env, "additional_cooling_fan_speed", n, 0)[i]
                                    for i in ids)
    vendors = _fil_list(env, "filament_vendor", n, "")
    env["is_all_bbl_filament"] = all(str(vendors[i]).strip('"') == "Bambu Lab" for i in ids)
    return env


def nozzle_of(env: Mapping[str, Any], filament_id: int) -> int:
    """Logical nozzle (0 left, 1 right) of a 0-based filament id."""
    fmap = env.get("filament_map") or [1]
    return int(_num(fmap[min(filament_id, len(fmap) - 1)])) - 1


def physical_extruder(env: Mapping[str, Any], nozzle: int) -> int:
    """Physical extruder id of a logical nozzle (physical_extruder_map; X2D: 0 -> 1, 1 -> 0)."""
    pm = env.get("physical_extruder_map") or [0, 1]
    return int(_num(pm[min(nozzle, len(pm) - 1)]))


def _at_nozzle(env: Mapping[str, Any], key: str, nozzle: int) -> Any:
    v = env.get(key)
    if isinstance(v, list):
        return v[min(nozzle, len(v) - 1)] if v else 0
    return v


def layer_temp(env: Mapping[str, Any], filament_id: int, layer_num: int) -> Any:
    """Nozzle temperature of a filament on a layer: nozzle_temperature_initial_layer on layer 0,
    nozzle_temperature after."""
    key = "nozzle_temperature_initial_layer" if layer_num == 0 else "nozzle_temperature"
    return env[key][filament_id]


def e_feedrate(env: Mapping[str, Any], filament_id: int) -> int:
    """Flush feed rate in mm/min of filament, as the change template's
    old/new_filament_e_feedrate: flush_volumetric_speeds / 2.4053 * 60, truncated. SOURCE: 299 for
    12 mm3/s, 523 for 21 mm3/s."""
    return int(_num(env["flush_volumetric_speeds"][filament_id]) / TEMPLATE_FILAMENT_AREA * 60.0)


def start_env(base: Mapping[str, Any], *, initial_filament_id: int,
              first_layer_print_min: Sequence[float], first_layer_print_size: Sequence[float],
              max_print_z: float, first_filaments: Sequence[int],
              first_non_support_filaments: Sequence[int] | None = None,
              initial_no_support_filament_id: int | None = None,
              initial_nozzle_id: int | None = None, initial_no_support_hotend: int = -1,
              hold_chamber_temp_for_flat_print: bool = False) -> ChainMap[str, Any]:
    """Variables for machine_start_gcode. Filament ids are 0-based; `first_filaments` and
    `first_non_support_filaments` are indexed by physical extruder id (see physical_extruder),
    -1 where that extruder prints nothing."""
    init_ns = initial_filament_id if initial_no_support_filament_id is None \
        else initial_no_support_filament_id
    ev: dict[str, Any] = {
        "initial_filament_id": initial_filament_id,
        "initial_extruder": initial_filament_id,
        "initial_no_support_filament_id": init_ns,
        "initial_no_support_extruder": init_ns,
        "initial_nozzle_id": nozzle_of(base, init_ns) if initial_nozzle_id is None
        else initial_nozzle_id,
        "initial_no_support_hotend": initial_no_support_hotend,
        "first_layer_print_min": [float(x) for x in first_layer_print_min],
        "first_layer_print_size": [float(x) for x in first_layer_print_size],
        "max_print_z": float(max_print_z),
        "first_filaments": [int(x) for x in first_filaments],
        "first_non_support_filaments": [int(x) for x in (first_non_support_filaments
                                                         or first_filaments)],
        "hold_chamber_temp_for_flat_print": bool(hold_chamber_temp_for_flat_print),
    }
    return ChainMap(ev, base)


def change_env(base: Mapping[str, Any], *, current_filament_id: int, next_filament_id: int,
               layer_num: int, layer_z: float, max_layer_z: float, toolchange_count: int,
               flush_length: float, z_after_toolchange: float,
               old_filament_temp: int | None = None, new_filament_temp: int | None = None,
               old_filament_e_feedrate: int | None = None,
               new_filament_e_feedrate: int | None = None,
               filament_cooling_before_tower: float | None = None,
               current_nozzle_id: int | None = None, next_nozzle_id: int | None = None,
               current_hotend: int = -1, next_hotend: int = -1,
               x_after_toolchange: float = 0.0, y_after_toolchange: float = 0.0,
               travel_point_1_x: float = 54, travel_point_1_y: float = 0,
               travel_point_2_x: float = 54, travel_point_2_y: float = 0,
               travel_point_3_x: float = 54, travel_point_3_y: float = 245,
               is_prime_tower_interface: bool = False,
               filament_tower_interface_purge_volume: float | None = None,
               filament_tower_interface_print_temp: int | None = None) -> ChainMap[str, Any]:
    """Variables for change_filament_gcode (one filament change). Filament ids 0-based.

    Defaults come from the config: temperatures for the layer (layer_temp), feed rates
    (e_feedrate), nozzles from filament_map, the long-retraction switches of the current
    filament, the next nozzle's initial_layer_acceleration / travel_acceleration.
    `filament_cooling_before_tower`: the next filament's value (None: 0 on the first layer and
    on a prime tower interface change, the config value otherwise; SOURCE: `M620.15 C` equals
    the new temperature on the 11 first-layer changes and the one interface change, and is 10
    below it on the other 3,151).
    """
    cur, nxt = current_filament_id, next_filament_id
    cn = nozzle_of(base, cur) if current_nozzle_id is None else current_nozzle_id
    nn = nozzle_of(base, nxt) if next_nozzle_id is None else next_nozzle_id
    ev: dict[str, Any] = {
        "current_filament_id": cur, "next_filament_id": nxt,
        "previous_extruder": cur, "next_extruder": nxt,
        "current_nozzle_id": cn, "next_nozzle_id": nn,
        "current_hotend": current_hotend, "next_hotend": next_hotend,
        "layer_num": int(layer_num), "layer_z": float(layer_z),
        "max_layer_z": float(max_layer_z), "toolchange_count": int(toolchange_count),
        "flush_length": float(flush_length),
        "z_after_toolchange": float(z_after_toolchange),
        "x_after_toolchange": float(x_after_toolchange),
        "y_after_toolchange": float(y_after_toolchange),
        "old_filament_temp": layer_temp(base, cur, layer_num) if old_filament_temp is None
        else old_filament_temp,
        "new_filament_temp": layer_temp(base, nxt, layer_num) if new_filament_temp is None
        else new_filament_temp,
        "old_filament_e_feedrate": e_feedrate(base, cur) if old_filament_e_feedrate is None
        else old_filament_e_feedrate,
        "new_filament_e_feedrate": e_feedrate(base, nxt) if new_filament_e_feedrate is None
        else new_filament_e_feedrate,
        "travel_point_1_x": travel_point_1_x, "travel_point_1_y": travel_point_1_y,
        "travel_point_2_x": travel_point_2_x, "travel_point_2_y": travel_point_2_y,
        "travel_point_3_x": travel_point_3_x, "travel_point_3_y": travel_point_3_y,
        "is_prime_tower_interface": bool(is_prime_tower_interface),
        "filament_tower_interface_purge_volume": (
            base.get("filament_tower_interface_purge_volume", [0.0])[nxt]
            if filament_tower_interface_purge_volume is None
            else filament_tower_interface_purge_volume),
        "filament_tower_interface_print_temp": (
            base.get("filament_tower_interface_print_temp", [0])[nxt]
            if filament_tower_interface_print_temp is None
            else filament_tower_interface_print_temp),
        "initial_layer_acceleration": _at_nozzle(base, "initial_layer_acceleration", nn),
        "travel_acceleration": _at_nozzle(base, "travel_acceleration", nn),
    }
    # Long retraction of the filament being unloaded.
    cut_on = _at_nozzle(base, "long_retractions_when_cut", cn)
    fcut = base.get("filament_retraction_distances_when_cut")
    cut_d = fcut[cur] if isinstance(fcut, list) and fcut[cur] is not NIL else \
        _at_nozzle(base, "retraction_distances_when_cut", cn)
    ev["long_retraction_when_cut"] = _truth(cut_on) if cut_on is not None else False
    ev["retraction_distance_when_cut"] = cut_d
    ec_on = base.get("long_retractions_when_ec")
    ec_d = base.get("retraction_distances_when_ec")
    ev["long_retraction_when_ec"] = _truth(ec_on[cur]) if isinstance(ec_on, list) else False
    ev["retraction_distance_when_ec"] = ec_d[cur] if isinstance(ec_d, list) else 0
    if filament_cooling_before_tower is None and (layer_num == 0 or is_prime_tower_interface):
        filament_cooling_before_tower = 0.0
    if filament_cooling_before_tower is not None:
        fct = list(base.get("filament_cooling_before_tower") or [0.0])
        fct[nxt] = filament_cooling_before_tower
        ev["filament_cooling_before_tower"] = fct
    return ChainMap(ev, base)


def layer_change_env(base: Mapping[str, Any], *, layer_num: int, layer_z: float,
                     max_layer_z: float, current_filament_id: int,
                     current_nozzle_id: int | None = None) -> ChainMap[str, Any]:
    """Variables for layer_change_gcode: the layer being started (0-based) and the filament
    loaded when it starts."""
    ev = {
        "layer_num": int(layer_num), "layer_z": float(layer_z),
        "max_layer_z": float(max_layer_z), "current_filament_id": current_filament_id,
        "current_extruder": current_filament_id,
        "current_nozzle_id": nozzle_of(base, current_filament_id) if current_nozzle_id is None
        else current_nozzle_id,
    }
    return ChainMap(ev, base)


def timelapse_env(base: Mapping[str, Any], *, layer_num: int, layer_z: float,
                  max_layer_z: float, curr_physical_extruder_id: int,
                  most_used_physical_extruder_id: int, has_timelapse_safe_pos: bool,
                  timelapse_pos_x: float = 0.0, timelapse_pos_y: float = 0.0,
                  timelapse_inline_photo: bool = False) -> ChainMap[str, Any]:
    """Variables for time_lapse_gcode."""
    ev = {
        "layer_num": int(layer_num), "layer_z": float(layer_z),
        "max_layer_z": float(max_layer_z),
        "curr_physical_extruder_id": int(curr_physical_extruder_id),
        "most_used_physical_extruder_id": int(most_used_physical_extruder_id),
        "has_timelapse_safe_pos": bool(has_timelapse_safe_pos),
        "timelapse_pos_x": timelapse_pos_x, "timelapse_pos_y": timelapse_pos_y,
        "timelapse_inline_photo": bool(timelapse_inline_photo),
        "farthest_point_timelapse_enabled": _truth(base.get("farthest_point_timelapse", 0)),
    }
    return ChainMap(ev, base)


def end_env(base: Mapping[str, Any], *, max_layer_z: float, current_filament_id: int,
            layer_num: int = 0, layer_z: float | None = None) -> ChainMap[str, Any]:
    """Variables for machine_end_gcode: the last layer's z and the filament loaded at the end
    (its filament_end_gcode precedes the end text)."""
    ev = {"max_layer_z": float(max_layer_z), "layer_num": int(layer_num),
          "layer_z": float(max_layer_z if layer_z is None else layer_z),
          "current_filament_id": current_filament_id, "current_extruder": current_filament_id}
    return ChainMap(ev, base)


def filament_start_end_env(base: Mapping[str, Any], *, filament_id: int, layer_num: int = 0,
                           layer_z: float = 0.0, max_layer_z: float = 0.0) -> ChainMap[str, Any]:
    """Variables for filament_start_gcode / filament_end_gcode of one filament (the X2D
    templates are plain comments and read none)."""
    ev = {"current_filament_id": filament_id, "current_extruder": filament_id,
          "layer_num": int(layer_num), "layer_z": float(layer_z),
          "max_layer_z": float(max_layer_z)}
    return ChainMap(ev, base)


def most_used_physical_extruder(env: Mapping[str, Any],
                                layer_nozzles: Sequence[Sequence[int]]) -> int:
    """most_used_physical_extruder_id: the physical extruder that prints (model, not tower) on
    the most layers, the lower id on a tie. `layer_nozzles`: per layer, the logical nozzles that
    print model there. SOURCE: matches the timelapse `E` of all 19 truth cases that have one
    (extrusion volume does not: it picks the other extruder on nameplate_5c_020 and
    meshy_retarget_3c_020)."""
    count = [0, 0]
    for nozzles in layer_nozzles:
        for p in {physical_extruder(env, nz) for nz in nozzles}:
            count[p] += 1
    return 0 if count[0] >= count[1] else 1


# ----------------------------------------------------------------------------- placement

# Lines Bambu writes into the G-code after the templates are placed (time-based inserts, not
# template output): progress, and the idle nozzle's pre-cooling / pre-heating / tower reheat.
_PROGRESS = re.compile(r"M73 (?:P\d+ R\d+|E\d+)\s*\Z")
_PRE_COOL = re.compile(r"M104 T\d+ S\d+ N0 ;Multi extruder pre cooling\s*\Z")
_PRE_HEAT = re.compile(r"M104 T\d+ S\d+ N0 ;Multi extruder pre heating\s*\Z")
_REHEAT = re.compile(r"M104 T\d+ S\d+ N0 ;Wipe tower reheat before wipe\s*\Z")
_M632 = re.compile(r"M632 S\d+ W\s*\Z")


def strip_inserted(lines: Sequence[str]) -> list[str]:
    """Drop the lines Bambu inserts after placing templates: `M73 P.. R..` and `M73 E..`
    progress; `M400` + `M104 T.. S.. N0 ;Multi extruder pre cooling`; `M632 S.. W` +
    `M104 .. ;Multi extruder pre heating` + `M633`; `M104 .. ;Wipe tower reheat before wipe`.
    (`M73 L..` is template output and stays.)"""
    out: list[str] = []
    skip_m633 = False
    for line in lines:
        if _PROGRESS.match(line) or _REHEAT.match(line):
            continue
        if _PRE_COOL.match(line):
            if out and out[-1].strip() == "M400":
                out.pop()
            continue
        if _M632.match(line):
            skip_m633 = True
            continue
        if _PRE_HEAT.match(line):
            continue
        if skip_m633 and line.strip() == "M633":
            skip_m633 = False
            continue
        out.append(line)
    return out


_FEED = re.compile(r"^(G[0-3](?:\s.*?)?)\s+F(\d+(?:\.\d*)?)(\s*(?:;.*)?)$")


def drop_repeated_feedrate(text: str, feedrate: float | None) -> tuple[str, float | None]:
    """Remove ` F<f>` from G0-G3 lines whose feed rate is already in effect, as Bambu does to the
    change_filament_gcode text (the second `G1 Z.. F3000` of the X2D change prints as `G1 Z..`).
    `feedrate`: the feed rate in effect before the block (mm/min). Returns the text and the feed
    rate in effect after it. SOURCE: all change blocks of the 20 truth cases; the timelapse,
    layer change, start and end blocks keep their repeated F values."""
    out = []
    for line in text.split("\n"):
        m = _FEED.match(line)
        if m:
            f = float(m.group(2))
            if feedrate is not None and f == feedrate:
                line = m.group(1) + m.group(3)
            feedrate = f
        out.append(line)
    return "\n".join(out), feedrate


CHANGE_TAIL_MARKER = ";not set fan changing filament"


def place_start(start_gcode: str, filament_start_gcode: str) -> str:
    """Rendered machine_start_gcode and the initial filament's filament_start_gcode as Bambu
    writes them: `; FEATURE: Custom`, the start text, `; MACHINE_START_GCODE_END`, the filament
    start text. Bambu's own lines follow (`;VT<n> H-1`, `G90`, `G21`, `M83 ; use relative
    distances for extrusion`, `M981 S1 P20000 ;open spaghetti detector`, `M204 S..`, the first
    `G1 Z.. F..` travel, then `; CHANGE_LAYER`)."""
    return f"; FEATURE: Custom\n{start_gcode}; MACHINE_START_GCODE_END\n{filament_start_gcode}"


def place_change(filament_end_gcode: str, change_filament_gcode: str,
                 feedrate: float | None) -> tuple[str, float | None]:
    """Rendered filament_end_gcode (old filament) and change_filament_gcode as Bambu writes them:
    the filament end text, a newline, the change text with repeated feed rates dropped
    (drop_repeated_feedrate) and its last line `;not set fan changing filament` removed.
    Before it Bambu writes the ramming block (`; NOZZLE_CHANGE_START OF.. NF.. ON.. NN..` on a
    nozzle switch), wipe, retract and lift; after it the fan command (`M106 S..`), a travel to
    the tower, then `; filament start gcode` of the new filament."""
    text, f = drop_repeated_feedrate(change_filament_gcode, feedrate)
    text = text.removesuffix(CHANGE_TAIL_MARKER)
    return f"{filament_end_gcode}\n{text}", f


def place_layer_change(layer_change_gcode: str) -> str:
    """Rendered layer_change_gcode as Bambu writes it: after `; CHANGE_LAYER`, `; Z_HEIGHT: z`,
    `; LAYER_HEIGHT: h` and the retract, followed by one extra newline."""
    return layer_change_gcode + "\n"


def place_timelapse(time_lapse_gcode: str) -> str:
    """Rendered time_lapse_gcode as Bambu writes it, followed by one extra newline."""
    return time_lapse_gcode + "\n"


def place_end(filament_end_gcode: str, machine_end_gcode: str) -> str:
    """Rendered filament_end_gcode (last filament) and machine_end_gcode as Bambu writes them:
    `; FEATURE: Custom`, `; MACHINE_END_GCODE_START`, the filament end text, the end text. Before
    it Bambu writes `M106 S0`, `M106 P2 S0`, `M981 S0 P20000 ; close spaghetti detector`; after
    it `M73 P100 R0` and `; EXECUTABLE_BLOCK_END`."""
    return (f"; FEATURE: Custom\n; MACHINE_END_GCODE_START\n{filament_end_gcode}"
            f"{machine_end_gcode}")


# ----------------------------------------------------------------------------- render and place


def _fil_template(env: Mapping[str, Any], key: str, filament_id: int) -> str:
    v = env.get(key) or ""
    if isinstance(v, list):
        return str(v[min(filament_id, len(v) - 1)]) if v else ""
    return str(v)


def start_block(env: Mapping[str, Any]) -> str:
    """machine_start_gcode and the initial filament's filament_start_gcode, rendered with a
    start_env and wrapped as Bambu writes them (place_start)."""
    fs = _fil_template(env, "filament_start_gcode", env["initial_filament_id"])
    return place_start(render(env["machine_start_gcode"], env), render(fs, env))


def change_block(env: Mapping[str, Any], feedrate: float | None) -> tuple[str, float | None]:
    """The old filament's filament_end_gcode and change_filament_gcode, rendered with a
    change_env and placed as Bambu writes them (place_change). `feedrate`: feed rate in effect
    before the block. Returns the text and the feed rate in effect after it."""
    fe = _fil_template(env, "filament_end_gcode", env["current_filament_id"])
    return place_change(render(fe, env), render(env["change_filament_gcode"], env), feedrate)


def filament_start_block(env: Mapping[str, Any]) -> str:
    """filament_start_gcode of env's current_filament_id (filament_start_end_env). After a
    change Bambu writes it after the travel to the tower, preceded by a blank line."""
    return render(_fil_template(env, "filament_start_gcode", env["current_filament_id"]), env)


def layer_change_block(env: Mapping[str, Any]) -> str:
    """layer_change_gcode rendered with a layer_change_env and placed (place_layer_change)."""
    return place_layer_change(render(env["layer_change_gcode"], env))


def timelapse_block(env: Mapping[str, Any]) -> str:
    """time_lapse_gcode rendered with a timelapse_env and placed (place_timelapse)."""
    return place_timelapse(render(env["time_lapse_gcode"], env))


def end_block(env: Mapping[str, Any]) -> str:
    """The last filament's filament_end_gcode and machine_end_gcode, rendered with an end_env
    and placed (place_end)."""
    fe = _fil_template(env, "filament_end_gcode", env["current_filament_id"])
    return place_end(render(fe, env), render(env["machine_end_gcode"], env))
