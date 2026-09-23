#!/usr/bin/env python3
"""
symbolic.py -- a tiny hash-consed boolean expression package plus a symbolic
evaluator for sky130 standard-cell netlists.

Used by ``structure_analyze.py`` to lift the flat gate netlist extracted from
``puzzle.gds`` back into per-flop next-state equations written in terms of the
flop outputs and the primary inputs.  This is the "read the RTL off the
silicon" step; no SAT solver is involved.

Node representation
-------------------
Every node is an immutable tuple that is interned in a global table, so
structurally identical sub-expressions are the *same* Python object and
``is`` works as equality:

    ('c', 0) / ('c', 1)        constants
    ('v', name)                variable
    ('~', a)                   inversion (never applied to another '~')
    ('&', a, b, ...)           sorted, flattened conjunction
    ('|', a, b, ...)           sorted, flattened disjunction
    ('^', a, b, ...)           sorted, flattened xor with the sign pulled out

Local simplification (constant folding, idempotence, complementation,
absorption of duplicate xor terms) is applied at construction time.  That is
enough to make the puzzle's equations readable; it is deliberately *not* a
full SAT/BDD canonicaliser.
"""

from __future__ import annotations

import functools
import itertools

_INTERN: dict = {}


def _mk(t):
    return _INTERN.setdefault(t, t)


ZERO = _mk(('c', 0))
ONE = _mk(('c', 1))


def var(name):
    return _mk(('v', name))


def is_const(a):
    return a[0] == 'c'


def NOT(a):
    if a is ZERO:
        return ONE
    if a is ONE:
        return ZERO
    if a[0] == '~':
        return a[1]
    return _mk(('~', a))


def _key(a):
    """Deterministic sort key for operand lists."""
    return repr(a)


def AND(*args):
    terms = []
    for a in args:
        if a is ZERO:
            return ZERO
        if a is ONE:
            continue
        if a[0] == '&':
            terms.extend(a[1:])
        else:
            terms.append(a)
    uniq = []
    seen = set()
    for t in terms:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    for t in uniq:
        if NOT(t) in seen:
            return ZERO
    if not uniq:
        return ONE
    if len(uniq) == 1:
        return uniq[0]
    uniq.sort(key=_key)
    return _mk(('&',) + tuple(uniq))


def OR(*args):
    terms = []
    for a in args:
        if a is ONE:
            return ONE
        if a is ZERO:
            continue
        if a[0] == '|':
            terms.extend(a[1:])
        else:
            terms.append(a)
    uniq = []
    seen = set()
    for t in terms:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    for t in uniq:
        if NOT(t) in seen:
            return ONE
    if not uniq:
        return ZERO
    if len(uniq) == 1:
        return uniq[0]
    uniq.sort(key=_key)
    return _mk(('|',) + tuple(uniq))


def XOR(*args):
    inv = False
    terms = []
    for a in args:
        if a is ONE:
            inv = not inv
            continue
        if a is ZERO:
            continue
        if a[0] == '~':
            inv = not inv
            a = a[1]
        if a[0] == '^':
            terms.extend(a[1:])
        else:
            terms.append(a)
    # x ^ x == 0
    cnt = {}
    for t in terms:
        cnt[t] = cnt.get(t, 0) + 1
    terms = [t for t, c in cnt.items() if c % 2]
    if not terms:
        r = ZERO
    elif len(terms) == 1:
        r = terms[0]
    else:
        terms.sort(key=_key)
        r = _mk(('^',) + tuple(terms))
    return NOT(r) if inv else r


def MUX(s, a0, a1):
    """a1 when s else a0."""
    if s is ONE:
        return a1
    if s is ZERO:
        return a0
    if a0 is a1:
        return a0
    if a1 is ONE and a0 is ZERO:
        return s
    if a1 is ZERO and a0 is ONE:
        return NOT(s)
    if a1 is ONE:
        return OR(s, a0)
    if a0 is ZERO:
        return AND(s, a1)
    if a1 is ZERO:
        return AND(NOT(s), a0)
    if a0 is ONE:
        return OR(NOT(s), a1)
    return OR(AND(s, a1), AND(NOT(s), a0))


# --------------------------------------------------------------------------
# pretty printing
# --------------------------------------------------------------------------

_PREC = {'v': 4, 'c': 4, '~': 3, '&': 2, '^': 1, '|': 0}


def to_str(a, parent=-1):
    k = a[0]
    if k == 'c':
        return str(a[1])
    if k == 'v':
        return a[1]
    if k == '~':
        return '!' + to_str(a[1], 3)
    op = {'&': ' & ', '|': ' | ', '^': ' ^ '}[k]
    p = _PREC[k]
    s = op.join(to_str(x, p) for x in a[1:])
    return '(' + s + ')' if p < parent else s


def variables(a, acc=None):
    acc = set() if acc is None else acc
    stack = [a]
    seen = set()
    while stack:
        n = stack.pop()
        if id(n) in seen:
            continue
        seen.add(id(n))
        if n[0] == 'v':
            acc.add(n[1])
        elif n[0] != 'c':
            stack.extend(n[1:])
    return acc


def size(a):
    seen = set()
    stack = [a]
    n = 0
    while stack:
        x = stack.pop()
        if id(x) in seen:
            continue
        seen.add(id(x))
        n += 1
        if x[0] not in ('v', 'c'):
            stack.extend(x[1:])
    return n


def evaluate(a, env, memo=None):
    """Evaluate to 0/1 given env: {varname: 0/1}."""
    memo = {} if memo is None else memo
    key = id(a)
    if key in memo:
        return memo[key]
    k = a[0]
    if k == 'c':
        r = a[1]
    elif k == 'v':
        r = env[a[1]]
    elif k == '~':
        r = 1 - evaluate(a[1], env, memo)
    elif k == '&':
        r = 1
        for x in a[1:]:
            if not evaluate(x, env, memo):
                r = 0
                break
    elif k == '|':
        r = 0
        for x in a[1:]:
            if evaluate(x, env, memo):
                r = 1
                break
    elif k == '^':
        r = 0
        for x in a[1:]:
            r ^= evaluate(x, env, memo)
    else:
        raise ValueError(a)
    memo[key] = r
    return r


def substitute(a, mapping, memo=None):
    """Replace variables per ``mapping`` (name -> node)."""
    memo = {} if memo is None else memo
    if id(a) in memo:
        return memo[id(a)]
    k = a[0]
    if k == 'c':
        r = a
    elif k == 'v':
        r = mapping.get(a[1], a)
    elif k == '~':
        r = NOT(substitute(a[1], mapping, memo))
    elif k == '&':
        r = AND(*[substitute(x, mapping, memo) for x in a[1:]])
    elif k == '|':
        r = OR(*[substitute(x, mapping, memo) for x in a[1:]])
    elif k == '^':
        r = XOR(*[substitute(x, mapping, memo) for x in a[1:]])
    else:
        raise ValueError(a)
    memo[id(a)] = r
    return r


def cofactor(a, name, value):
    return substitute(a, {name: ONE if value else ZERO})
