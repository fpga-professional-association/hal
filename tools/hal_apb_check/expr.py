"""Single-bit Boolean terms used by every layer of the APB checker.

A term is an immutable tuple, which makes it hashable, structurally shared and
trivially comparable::

    ("const", True) | ("var", name) | ("not", term)
    ("and", (t, ...)) | ("or", (t, ...)) | ("xor", (t, t))

Constructors fold constants and flatten associative operators eagerly. That is
not an optimisation for its own sake: the bounded model checker instantiates
every property at every cycle, so a term that is trivially ``False`` (a property
whose antecedent mentions a signal tied to ground) must collapse *before* it
reaches the SAT encoder, otherwise the "is this check vacuous?" answer would
depend on how hard the solver tried.

Nothing here knows about APB, netlists or ``hal_py``.
"""

__all__ = [
    "TRUE",
    "FALSE",
    "var",
    "const",
    "not_",
    "and_",
    "or_",
    "xor_",
    "implies",
    "iff",
    "ite",
    "is_const",
    "const_value",
    "variables",
    "evaluate",
    "substitute",
    "to_string",
    "to_smt2",
]

TRUE = ("const", True)
FALSE = ("const", False)


def const(value):
    """A constant term."""
    return TRUE if value else FALSE


def var(name):
    """A free single-bit variable."""
    if not isinstance(name, str) or not name:
        raise ValueError("variable name must be a non-empty string, got {!r}".format(name))
    return ("var", name)


def is_const(term):
    return term[0] == "const"


def const_value(term):
    if term[0] != "const":
        raise ValueError("not a constant term: {}".format(to_string(term)))
    return term[1]


def not_(term):
    if term[0] == "const":
        return const(not term[1])
    if term[0] == "not":
        return term[1]
    return ("not", term)


def _flatten(op, terms):
    out = []
    for term in terms:
        if term[0] == op:
            out.extend(term[1])
        else:
            out.append(term)
    return out


def _dedup(terms):
    seen = set()
    out = []
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        out.append(term)
    return out


def _as_terms(terms):
    """Accept either ``op(a, b)`` or ``op([a, b])``.

    A term is always a tuple whose first element is a *string* tag, so a single
    argument that is a sequence of something else is a sequence of terms.
    """
    if len(terms) == 1 and isinstance(terms[0], (list, tuple)):
        only = terms[0]
        if len(only) == 0 or not isinstance(only[0], str):
            return tuple(only)
    return terms


def and_(*terms):
    """Conjunction; ``and_()`` is ``TRUE``."""
    terms = _as_terms(terms)
    flat = _dedup(_flatten("and", terms))
    kept = []
    for term in flat:
        if term == TRUE:
            continue
        if term == FALSE:
            return FALSE
        kept.append(term)
    negations = {not_(term) for term in kept}
    if negations & set(kept):
        return FALSE
    if not kept:
        return TRUE
    if len(kept) == 1:
        return kept[0]
    return ("and", tuple(kept))


def or_(*terms):
    """Disjunction; ``or_()`` is ``FALSE``."""
    terms = _as_terms(terms)
    flat = _dedup(_flatten("or", terms))
    kept = []
    for term in flat:
        if term == FALSE:
            continue
        if term == TRUE:
            return TRUE
        kept.append(term)
    negations = {not_(term) for term in kept}
    if negations & set(kept):
        return TRUE
    if not kept:
        return FALSE
    if len(kept) == 1:
        return kept[0]
    return ("or", tuple(kept))


def xor_(left, right):
    if left[0] == "const":
        return right if not left[1] else not_(right)
    if right[0] == "const":
        return left if not right[1] else not_(left)
    if left == right:
        return FALSE
    if left == not_(right):
        return TRUE
    return ("xor", (left, right))


def implies(antecedent, consequent):
    return or_(not_(antecedent), consequent)


def iff(left, right):
    return not_(xor_(left, right))


def ite(condition, then_term, else_term):
    if condition[0] == "const":
        return then_term if condition[1] else else_term
    if then_term == else_term:
        return then_term
    return or_(and_(condition, then_term), and_(not_(condition), else_term))


def variables(term, into=None):
    """Sorted list of variable names occurring in ``term``."""
    names = set() if into is None else into
    stack = [term]
    while stack:
        node = stack.pop()
        kind = node[0]
        if kind == "var":
            names.add(node[1])
        elif kind == "not":
            stack.append(node[1])
        elif kind in ("and", "or", "xor"):
            stack.extend(node[1])
    return names if into is not None else sorted(names)


def evaluate(term, assignment):
    """Evaluate ``term`` under ``assignment`` (name -> bool).

    A variable missing from ``assignment`` raises :class:`KeyError`; the checker
    never silently treats an unknown signal as ``0``.
    """
    kind = term[0]
    if kind == "const":
        return term[1]
    if kind == "var":
        return bool(assignment[term[1]])
    if kind == "not":
        return not evaluate(term[1], assignment)
    if kind == "and":
        return all(evaluate(operand, assignment) for operand in term[1])
    if kind == "or":
        return any(evaluate(operand, assignment) for operand in term[1])
    if kind == "xor":
        return evaluate(term[1][0], assignment) != evaluate(term[1][1], assignment)
    raise ValueError("unknown term kind {!r}".format(kind))


def substitute(term, mapping):
    """Replace variables by terms (``mapping`` maps name -> term)."""
    kind = term[0]
    if kind == "const":
        return term
    if kind == "var":
        return mapping.get(term[1], term)
    if kind == "not":
        return not_(substitute(term[1], mapping))
    if kind == "and":
        return and_(*[substitute(operand, mapping) for operand in term[1]])
    if kind == "or":
        return or_(*[substitute(operand, mapping) for operand in term[1]])
    if kind == "xor":
        return xor_(substitute(term[1][0], mapping), substitute(term[1][1], mapping))
    raise ValueError("unknown term kind {!r}".format(kind))


def to_string(term):
    kind = term[0]
    if kind == "const":
        return "1" if term[1] else "0"
    if kind == "var":
        return term[1]
    if kind == "not":
        return "!" + to_string(term[1])
    if kind == "and":
        return "(" + " & ".join(to_string(operand) for operand in term[1]) + ")"
    if kind == "or":
        return "(" + " | ".join(to_string(operand) for operand in term[1]) + ")"
    if kind == "xor":
        return "({} ^ {})".format(to_string(term[1][0]), to_string(term[1][1]))
    raise ValueError("unknown term kind {!r}".format(kind))


def _smt2_term(term):
    kind = term[0]
    if kind == "const":
        return "true" if term[1] else "false"
    if kind == "var":
        return "|{}|".format(term[1])
    if kind == "not":
        return "(not {})".format(_smt2_term(term[1]))
    if kind in ("and", "or"):
        return "({} {})".format(kind, " ".join(_smt2_term(operand) for operand in term[1]))
    if kind == "xor":
        return "(xor {} {})".format(_smt2_term(term[1][0]), _smt2_term(term[1][1]))
    raise ValueError("unknown term kind {!r}".format(kind))


def to_smt2(assertions, comment=None, get_model=True):
    """Render a satisfiability query as SMT-LIB 2 text.

    The checker's own solver decides the query; this export exists so a result
    can be re-derived with an independent solver (``z3 query.smt2``). It is
    written as *evidence*, not as a second opinion the tool silently trusts.
    """
    names = set()
    for assertion in assertions:
        variables(assertion, into=names)
    lines = []
    if comment:
        for line in str(comment).splitlines():
            lines.append("; {}".format(line))
    lines.append("(set-logic QF_UF)")
    if get_model:
        lines.append("(set-option :produce-models true)")
    for name in sorted(names):
        lines.append("(declare-fun |{}| () Bool)".format(name))
    for assertion in assertions:
        lines.append("(assert {})".format(_smt2_term(assertion)))
    lines.append("(check-sat)")
    if get_model:
        lines.append("(get-model)")
    return "\n".join(lines) + "\n"
