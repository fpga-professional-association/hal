#!/usr/bin/env python3
"""Property/fuzz harness for hal_py.BooleanFunction.

Random single-bit expression trees over at most 6 variables are built with the
``BooleanFunction.Var/Const/Not/And/Or/Xor`` constructors and then checked
against four properties, exhaustively over all 2**n input assignments:

  (0) model agreement -- HAL's ``compute_truth_table()`` matches a truth table
      computed independently in plain Python from the same expression tree.
      (Extra oracle, not strictly required, but it is what turns a disagreement
      into an actionable finding.)
  (a) print/parse round trip -- ``BooleanFunction.from_string(str(f))`` has the
      same truth table as ``f``.
  (a') every documented alternative spelling parses back to the same function,
      both fully bracketed and with the minimum number of brackets the
      documented precedence (NOT > AND > XOR > OR) allows.
  (b) ``simplify()`` preserves the truth table, and its output still parses.
  (c) double negation ``Not(Not(f))`` preserves the truth table.

Everything is deterministic: seeds come from a fixed list, never from the clock.

Usage
-----
    python3 tests/fuzz/fuzz_boolean_function.py

    FUZZ_SEED=12345 python3 tests/fuzz/fuzz_boolean_function.py   # one-off repro
    FUZZ_ITERS=200  python3 tests/fuzz/fuzz_boolean_function.py   # deeper sweep
    FUZZ_SAMPLES=50 python3 tests/fuzz/fuzz_boolean_function.py   # trees per seed

    # re-enable the spelling combination that only reproduces the already-known
    # BF-2 defect (0b-constants together with whitespace-AND), off by default so
    # the sweep keeps hunting for *new* bugs:
    FUZZ_ENABLE_KNOWN_BUG_TRIGGERS=1 python3 tests/fuzz/fuzz_boolean_function.py

The harness exits 0 as long as every failure it sees is an *expected* one, so it
can gate CI. Unexpected failures exit 1 and print a one-line repro command.
"""

import os
import random
import sys
import traceback
from pathlib import Path

# --------------------------------------------------------------------------- #
# hal_py bootstrap
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_hal_py():
    try:
        import hal_py  # noqa: F401
        return hal_py
    except ImportError:
        pass
    candidates = []
    for env in ("HAL_PY_PATH", "PYTHONPATH"):
        for part in os.environ.get(env, "").split(os.pathsep):
            if part:
                candidates.append(Path(part))
    base = os.environ.get("HAL_BASE_PATH")
    if base:
        candidates.append(Path(base) / "lib")
    candidates.append(REPO_ROOT / "build" / "lib")
    for cand in candidates:
        if (cand / "hal_py.so").exists() or (cand / "hal_py.dylib").exists():
            sys.path.insert(0, str(cand))
            break
    import hal_py  # noqa: F811
    return hal_py


if not os.environ.get("HAL_BASE_PATH") and (REPO_ROOT / "build").is_dir():
    # hal aborts hard ("cannot determine base path") when it is neither
    # installed nor told where the build tree is
    os.environ["HAL_BASE_PATH"] = str(REPO_ROOT / "build")

hal_py = _import_hal_py()
hal_py.plugin_manager.load_all_plugins()

BF = hal_py.BooleanFunction
ZERO = BF.Value.ZERO
ONE = BF.Value.ONE


def bf_str(function):
    """String form of a BooleanFunction.

    NOTE (BF-1, binding gap): ``BooleanFunction::to_string()`` has no Python
    binding of its own. ``src/python_bindings/bindings/boolean_function.cpp``
    only exposes the *static* ``to_string(value: list[Value], base: int)``
    bit-vector formatter under that name, so ``f.to_string()`` raises
    ``TypeError: incompatible function arguments`` for every instance -- the
    instance method is reachable only through ``__str__``. Prefer the real
    method once it exists so this harness keeps testing the documented API.
    """
    try:
        return function.to_string()
    except TypeError:
        return str(function)

# --------------------------------------------------------------------------- #
# seeds / configuration
# --------------------------------------------------------------------------- #

# 25 fixed seeds -- never derived from wall-clock time.
DEFAULT_SEEDS = [
    1, 2, 3, 5, 8, 13, 21, 34, 55, 89,
    1009, 2027, 3041, 4051, 5077, 6091, 7109, 8123, 9137, 10151,
    424242, 606060, 808080, 987654, 1337133,
]

MAX_VARS = 6
SAMPLES_PER_SEED = int(os.environ.get("FUZZ_SAMPLES", "40"))

# Variable naming styles. "plain" is the boring case; "indexed" exercises the
# ``A(0)`` spelling HAL uses for the bits of a pin group, which the Boolean
# function parser has to accept as a single variable name.
NAME_STYLES = ("plain", "indexed")

ENABLE_KNOWN_BUG_TRIGGERS = os.environ.get("FUZZ_ENABLE_KNOWN_BUG_TRIGGERS", "") not in ("", "0")

# Seeds that are known to fail. Empty: with the known-bug triggers off, all 25
# default seeds pass. Add entries as ``seed: "reason"`` when a new real failure
# is found and minimized; the harness then keeps exiting 0 while still
# reporting the finding.
EXPECTED_FAILURE_SEEDS = {}


def _probe_bf1():
    """BooleanFunction::to_string() has no instance binding."""
    try:
        BF.Var("A", 1).to_string()
    except TypeError:
        return True
    return False


def _probe_bf2a():
    """Whitespace AND next to a 0b-constant is silently glued into a variable."""
    parsed = BF.from_string("A 0b0")
    return (not parsed.is_empty()) and parsed.is_variable() \
        and parsed.has_variable_name("A0b0")


def _probe_bf2b():
    """Whitespace AND anywhere makes every 0b-constant in the string unparseable."""
    return BF.from_string("(A B) & 0b1").is_empty()


# Minimized repros of already-known defects, run on every invocation as
# expected failures. If one of them stops reproducing, the harness says so (and
# still exits 0) so the entry can be removed.
KNOWN_BUGS = [
    {
        "id": "BF-1",
        "summary": (
            "Binding gap: src/python_bindings/bindings/boolean_function.cpp binds "
            "BooleanFunction::to_string() only as __str__, while the name 'to_string' is "
            "taken by the static bit-vector formatter to_string(value, base). "
            "f.to_string() therefore raises TypeError for every instance, and Python code "
            "following the C++ API has to use str(f) instead."
        ),
        "probe": _probe_bf1,
    },
    {
        "id": "BF-2a",
        "summary": (
            "SILENT MISPARSE. BooleanFunction::from_string() tries Standard, then Liberty, "
            "then 'LibertyNoSpace' -- which strips *all* spaces and re-parses. The Liberty "
            "grammar does not know the 0b0/0b1 constants that from_string()'s own docstring "
            "documents, so 'A 0b0' falls through to the no-space pass, becomes the single "
            "token 'A0b0' and is returned as a *variable named A0b0* -- no error, wrong "
            "function. The space-stripping fallback can silently turn an AND of two "
            "operands into one concatenated identifier."
        ),
        "probe": _probe_bf2a,
    },
    {
        "id": "BF-2b",
        "summary": (
            "Same root cause, loud variant: once whitespace-AND is used anywhere, "
            "from_string() is on the Liberty grammar and every 0b0/0b1 in the expression "
            "makes the whole parse fail. '(A B) & 0b1' returns 'no parser available', "
            "while '(A B) & 1' and 'A & 0b1' are both fine. The two documented spellings "
            "(whitespace for AND, 0b0/0b1 for constants) are mutually exclusive."
        ),
        "probe": _probe_bf2b,
    },
]


def seed_sequence(count):
    seeds = list(DEFAULT_SEEDS)
    extra = random.Random(0xB001EA07)
    while len(seeds) < count:
        seeds.append(extra.randrange(1, 2 ** 31))
    return seeds[:count]


# --------------------------------------------------------------------------- #
# expression trees
# --------------------------------------------------------------------------- #
#
# A tree is a plain tuple so that it can be shrunk without touching hal_py:
#   ("var", name) | ("const", 0|1) | ("not", t) | ("and"|"or"|"xor", t, t)

BINARY_OPS = ("and", "or", "xor")


def random_tree(rnd, variables, depth):
    if depth <= 0 or rnd.random() < 0.22:
        if rnd.random() < 0.12:
            return ("const", rnd.randint(0, 1))
        return ("var", rnd.choice(variables))
    kind = rnd.choice(["not"] + list(BINARY_OPS) + list(BINARY_OPS))
    if kind == "not":
        return ("not", random_tree(rnd, variables, depth - 1))
    return (kind,
            random_tree(rnd, variables, depth - 1),
            random_tree(rnd, variables, depth - 1))


def tree_size(tree):
    if tree[0] in ("var", "const"):
        return 1
    return 1 + sum(tree_size(child) for child in tree[1:])


def tree_variables(tree):
    if tree[0] == "var":
        return {tree[1]}
    if tree[0] == "const":
        return set()
    out = set()
    for child in tree[1:]:
        out |= tree_variables(child)
    return out


def tree_repr(tree):
    if tree[0] == "var":
        return tree[1]
    if tree[0] == "const":
        return str(tree[1])
    if tree[0] == "not":
        return "!(%s)" % tree_repr(tree[1])
    symbol = {"and": "&", "or": "|", "xor": "^"}[tree[0]]
    return "(%s %s %s)" % (tree_repr(tree[1]), symbol, tree_repr(tree[2]))


# ---- alternative spellings ------------------------------------------------ #
#
# BooleanFunction.from_string documents these spellings, in decreasing order of
# precedence: NOT (``!``, ``~``, suffix ``'``), AND (``&``, ``*``, whitespace),
# XOR (``^``), OR (``|``, ``+``). Rendering the same tree with random spellings
# -- and, separately, with the minimum number of brackets the documented
# precedence allows -- is what exercises that grammar.

_PRECEDENCE = {"or": 1, "xor": 2, "and": 3, "not": 4, "var": 5, "const": 5}
_SPELLINGS = {
    "not": ("!", "~", "'"),
    "and": ("&", "*", " "),
    "xor": ("^",),
    "or": ("|", "+"),
}


def render(tree, rnd, minimal_parens, whitespace_and, binary_constants,
           parent_precedence=0):
    """Render the tree as a from_string()-compatible expression.

    ``whitespace_and`` allows the whitespace spelling of AND, ``binary_constants``
    allows the ``0b0`` / ``0b1`` spelling of constants. Both are documented by
    from_string(), but they cannot be combined -- see BF-2 in KNOWN_BUGS -- so
    the caller only turns both on when it wants to reproduce that bug.
    """
    kind = tree[0]
    if kind == "var":
        return tree[1]
    if kind == "const":
        if binary_constants:
            return rnd.choice(("0b%d" % tree[1], str(tree[1])))
        return str(tree[1])

    precedence = _PRECEDENCE[kind]
    if kind == "not":
        spelling = rnd.choice(_SPELLINGS["not"])
        inner = render(tree[1], rnd, minimal_parens, whitespace_and,
                       binary_constants, precedence)
        text = "%s%s" % (inner, spelling) if spelling == "'" else "%s%s" % (spelling, inner)
    else:
        choices = _SPELLINGS[kind]
        if kind == "and" and not whitespace_and:
            choices = tuple(c for c in choices if c != " ")
        spelling = rnd.choice(choices)
        left = render(tree[1], rnd, minimal_parens, whitespace_and,
                      binary_constants, precedence)
        right = render(tree[2], rnd, minimal_parens, whitespace_and,
                       binary_constants, precedence + 1)
        text = "%s%s%s" % (left, spelling, right) if spelling == " " \
            else "%s %s %s" % (left, spelling, right)

    if minimal_parens and precedence > parent_precedence:
        return text
    return "(%s)" % text


class BuildError(Exception):
    pass


def build(tree):
    """Turn the tree into a hal_py.BooleanFunction using the Var/And/... ctors."""
    kind = tree[0]
    if kind == "var":
        return BF.Var(tree[1], 1)
    if kind == "const":
        return BF.Const(ONE if tree[1] else ZERO)
    if kind == "not":
        result = BF.Not(build(tree[1]), 1)
        if result is None:
            raise BuildError("BooleanFunction.Not() returned None for %s" % tree_repr(tree))
        return result
    ctor = {"and": BF.And, "or": BF.Or, "xor": BF.Xor}[kind]
    result = ctor(build(tree[1]), build(tree[2]), 1)
    if result is None:
        raise BuildError("BooleanFunction.%s() returned None for %s"
                         % (kind.capitalize(), tree_repr(tree)))
    return result


def reference_eval(tree, assignment):
    """Independent Python evaluation of the tree (the reference model)."""
    kind = tree[0]
    if kind == "var":
        return assignment[tree[1]]
    if kind == "const":
        return tree[1]
    if kind == "not":
        return 1 - reference_eval(tree[1], assignment)
    left = reference_eval(tree[1], assignment)
    right = reference_eval(tree[2], assignment)
    if kind == "and":
        return left & right
    if kind == "or":
        return left | right
    return left ^ right


def reference_table(tree, variables):
    """Truth table in HAL's row order: variable i toggles every 2**i rows."""
    rows = []
    for index in range(2 ** len(variables)):
        assignment = {name: (index >> position) & 1
                      for position, name in enumerate(variables)}
        rows.append(reference_eval(tree, assignment))
    return rows


# --------------------------------------------------------------------------- #
# properties
# --------------------------------------------------------------------------- #


class PropertyFailure(Exception):
    def __init__(self, prop, detail):
        Exception.__init__(self, "%s: %s" % (prop, detail))
        self.prop = prop
        self.detail = detail


def hal_table(function, variables):
    table = function.compute_truth_table(variables, False)
    if table is None:
        raise PropertyFailure("truth-table", "compute_truth_table() returned None for %r"
                              % bf_str(function))
    if len(table) != 1:
        raise PropertyFailure("truth-table", "expected a single output column, got %d for %r"
                              % (len(table), bf_str(function)))
    return [1 if value == ONE else 0 if value == ZERO else str(value) for value in table[0]]


def check_tree(tree, variables, rnd=None):
    """Run every property on one expression tree. Raises PropertyFailure."""
    rnd = rnd if rnd is not None else random.Random(0xA11FEED)
    variables = sorted(variables or tree_variables(tree))
    if not variables:
        variables = ["UNUSED"]

    function = build(tree)
    expected = reference_table(tree, variables)
    actual = hal_table(function, variables)

    # (0) HAL agrees with the independent Python model
    if actual != expected:
        raise PropertyFailure(
            "model-agreement",
            "truth table of %r\n    hal      : %s\n    reference: %s\n    vars: %s"
            % (bf_str(function), actual, expected, variables))

    # (a) to_string -> from_string preserves the truth table
    printed = bf_str(function)
    reparsed = BF.from_string(printed)
    if reparsed.is_empty():
        raise PropertyFailure(
            "print-parse",
            "from_string() could not parse what to_string() produced: %r" % printed)
    if hal_table(reparsed, variables) != expected:
        raise PropertyFailure(
            "print-parse",
            "re-parsing changed the truth table\n    printed  : %r\n    re-parsed: %r\n"
            "    hal      : %s\n    expected : %s"
            % (printed, bf_str(reparsed), hal_table(reparsed, variables), expected))

    # (a') the documented alternative spellings parse to the same function,
    #      both fully bracketed and with the minimum brackets the documented
    #      precedence (NOT > AND > XOR > OR) allows
    for minimal_parens in (False, True):
        for whitespace_and in (False, True):
            # 0b-constants stay off in whitespace mode unless the caller asks
            # for the known-bug trigger (BF-2)
            binary_constants = (not whitespace_and) or ENABLE_KNOWN_BUG_TRIGGERS
            expression = render(tree, rnd, minimal_parens, whitespace_and,
                                binary_constants)
            label = "%s%s" % (
                " (minimal brackets)" if minimal_parens else "",
                " (whitespace AND)" if whitespace_and else "")
            alternative = BF.from_string(expression)
            if alternative.is_empty():
                raise PropertyFailure(
                    "alternative-spelling",
                    "from_string() rejected a documented spelling%s: %r" % (label, expression))
            if hal_table(alternative, variables) != expected:
                raise PropertyFailure(
                    "alternative-spelling",
                    "spelling%s changed the truth table\n    input   : %r\n    parsed  : %r\n"
                    "    hal     : %s\n    expected: %s"
                    % (label, expression, bf_str(alternative),
                       hal_table(alternative, variables), expected))

    # (b) simplify() preserves the truth table
    simplified = function.simplify()
    if hal_table(simplified, variables) != expected:
        raise PropertyFailure(
            "simplify",
            "simplify() changed the truth table\n    before: %r\n    after : %r\n"
            "    hal   : %s\n    expected: %s"
            % (printed, bf_str(simplified), hal_table(simplified, variables), expected))

    # (b') simplify() output must still print/parse round trip
    resimplified = BF.from_string(bf_str(simplified))
    if resimplified.is_empty():
        raise PropertyFailure(
            "simplify-print-parse",
            "from_string() could not parse simplify() output: %r" % bf_str(simplified))
    if hal_table(resimplified, variables) != expected:
        raise PropertyFailure(
            "simplify-print-parse",
            "re-parsing simplify() output changed the truth table\n    %r"
            % bf_str(simplified))

    # (c) double negation preserves the truth table
    negated = BF.Not(function.clone(), 1)
    if negated is None:
        raise PropertyFailure("double-negation", "Not() returned None")
    double = BF.Not(negated, 1)
    if double is None:
        raise PropertyFailure("double-negation", "Not() returned None on the second negation")
    if hal_table(double, variables) != expected:
        raise PropertyFailure(
            "double-negation",
            "!!f differs from f\n    f : %r\n    !!f: %r\n    hal: %s\n    expected: %s"
            % (printed, bf_str(double), hal_table(double, variables), expected))
    if hal_table(double.simplify(), variables) != expected:
        raise PropertyFailure(
            "double-negation",
            "simplify(!!f) differs from f\n    f : %r\n    simplified: %r"
            % (printed, bf_str(double.simplify())))
    return True


# --------------------------------------------------------------------------- #
# generation + minimization
# --------------------------------------------------------------------------- #


def make_variables(rnd):
    count = rnd.randint(1, MAX_VARS)
    style = rnd.choice(NAME_STYLES)
    if style == "indexed":
        base = rnd.choice(["A", "IN", "net"])
        return ["%s(%d)" % (base, i) for i in range(count)]
    return ["ABCDEF"[i] for i in range(count)]


def generate(seed, samples):
    rnd = random.Random(seed)
    out = []
    for _ in range(samples):
        variables = make_variables(rnd)
        depth = rnd.randint(1, 6)
        out.append((random_tree(rnd, variables, depth), variables))
    return out


def _still_fails(tree, variables, rnd_seed):
    try:
        check_tree(tree, variables, random.Random(rnd_seed))
        return False
    except PropertyFailure:
        return True
    except Exception:
        return True


def _subtree_replacements(tree):
    """Candidate shrinks: replace the whole tree by a child or by a leaf."""
    out = []
    if tree[0] in ("not", "and", "or", "xor"):
        out.extend(tree[1:])
    out.append(("const", 0))
    out.append(("const", 1))
    return out


def _positions(tree, prefix=()):
    yield prefix
    if tree[0] in ("not", "and", "or", "xor"):
        for index, child in enumerate(tree[1:], start=1):
            for pos in _positions(child, prefix + (index,)):
                yield pos


def _get(tree, position):
    for index in position:
        tree = tree[index]
    return tree


def _set(tree, position, replacement):
    if not position:
        return replacement
    index = position[0]
    children = list(tree)
    children[index] = _set(tree[index], position[1:], replacement)
    return tuple(children)


def minimize(tree, variables, rnd_seed, budget=500):
    """Greedy shrink: replace nodes by children/leaves while the failure holds."""
    current = tree
    steps = 0
    changed = True
    while changed and steps < budget:
        changed = False
        for position in list(_positions(current)):
            if steps >= budget:
                break
            node = _get(current, position)
            for replacement in _subtree_replacements(node):
                steps += 1
                candidate = _set(current, position, replacement)
                if tree_size(candidate) >= tree_size(current):
                    continue
                if _still_fails(candidate, variables, rnd_seed):
                    current = candidate
                    changed = True
                    break
            if changed:
                break
    return current


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def repro_command(seed):
    return "FUZZ_SEED=%d python3 tests/fuzz/fuzz_boolean_function.py" % seed


def run_seed(seed, samples):
    for index, (tree, variables) in enumerate(generate(seed, samples)):
        try:
            check_tree(tree, variables, random.Random(seed * 7919 + index))
        except PropertyFailure as failure:
            failure.tree = tree
            failure.variables = variables
            failure.rnd_seed = seed * 7919 + index
            raise
    return True


def run_known_bugs():
    """Re-check the minimized repros. Returns (still reproducing, fixed)."""
    xfail, xpass = [], []
    for bug in KNOWN_BUGS:
        try:
            reproduces = bool(bug["probe"]())
        except Exception as exc:  # pragma: no cover - defensive
            reproduces = True
            bug = dict(bug, summary=bug["summary"] + " [probe raised %s]" % exc)
        (xfail if reproduces else xpass).append(bug["id"])
    return xfail, xpass


def main():
    env_seed = os.environ.get("FUZZ_SEED")
    if env_seed:
        seeds = [int(env_seed, 0)]
    else:
        iters = int(os.environ.get("FUZZ_ITERS", str(len(DEFAULT_SEEDS))))
        seeds = seed_sequence(iters)

    print("fuzz_boolean_function: %d seeds x %d trees, <= %d variables, "
          "known-bug triggers %s"
          % (len(seeds), SAMPLES_PER_SEED, MAX_VARS,
             "ON" if ENABLE_KNOWN_BUG_TRIGGERS else "off"))

    unexpected = []
    expected = []
    passed = 0
    for seed in seeds:
        try:
            run_seed(seed, SAMPLES_PER_SEED)
        except PropertyFailure as failure:
            if seed in EXPECTED_FAILURE_SEEDS:
                expected.append((seed, failure.prop))
                continue
            unexpected.append((seed, failure))
            print("FAIL seed=%d property=%s" % (seed, failure.prop))
            print("     repro: %s" % repro_command(seed))
            print(failure.detail)
            try:
                small = minimize(failure.tree, failure.variables, failure.rnd_seed)
                print("--- minimized repro (seed %d) ---" % seed)
                print("     tree: %s" % tree_repr(small))
                print("     vars: %s" % sorted(failure.variables))
                print("     hal : %r" % bf_str(build(small)))
            except Exception:
                print("     (minimization failed)")
                traceback.print_exc()
        except Exception as exc:
            unexpected.append((seed, exc))
            print("ERROR seed=%d: %s" % (seed, exc))
            print("     repro: %s" % repro_command(seed))
            traceback.print_exc()
        else:
            if seed in EXPECTED_FAILURE_SEEDS:
                print("XPASS seed=%d unexpectedly passed -- drop it from "
                      "EXPECTED_FAILURE_SEEDS" % seed)
            passed += 1

    xfail, xpass = run_known_bugs()

    print("")
    if KNOWN_BUGS:
        print("known bugs still reproducing (expected failures): %s"
              % (", ".join(xfail) or "none"))
        if xpass:
            print("known bugs that NO LONGER reproduce -- update KNOWN_BUGS: %s"
                  % ", ".join(xpass))
    print("seeds: %d passed, %d expected-fail, %d UNEXPECTED"
          % (passed, len(expected), len(unexpected)))

    if unexpected:
        print("")
        print("unexpected failures (real findings, please escalate):")
        for seed, _ in unexpected:
            print("  %s" % repro_command(seed))
        return 1
    print("fuzz_boolean_function: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
