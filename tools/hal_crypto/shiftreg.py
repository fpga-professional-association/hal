"""Shift registers, LFSRs and NLFSRs.

A shift register is the easiest crypto-relevant structure to see in a netlist
and the easiest one to over-claim about, so this pass separates statements that
are usually run together:

* **there is a shift chain** -- registers wired ``q`` to ``d`` through nothing
  but buffers and inverters.  Structural, cheap, and says nothing about
  cryptography: a synchroniser and a debouncer are shift chains too;
* **the chain is closed by a feedback function** whose inputs are all stages of
  that same chain.  That is what makes it a *feedback* shift register;
* **the feedback is linear** (an XOR of taps, so the recurrence has a feedback
  polynomial) or **nonlinear** (the ANF has a term of degree >= 2, so it does
  not, and the honest report is the ANF itself);
* **the form is Fibonacci** (one feedback function into the head) or **Galois**
  (the last stage is XORed into several stages along the chain).

Storage polarity is a synthesis artefact, not a design choice.  Quartus
implements an asynchronous load of a non-zero seed by storing some stages
inverted and putting an inverter on the output, which turns half the shift
links into ``d = !q_prev``.  The pass therefore works in a *gauge*: stage *i*'s
logical value is ``q_i ^ c_i`` with ``c_0 = 0`` and ``c_i`` the running parity
of the inverters along the chain.  The tap set is invariant under that choice;
only the constant term of the feedback is not, and when the tap count is even
the remaining freedom (complementing the whole state) is used to normalise the
constant to zero.  Both facts are recorded in the result, because "we chose the
encoding that makes this a textbook LFSR" is part of the claim.
"""

from . import boolfunc
from .boolfunc import (
    polynomial_exponents,
    polynomial_from_exponents,
    reciprocal_exponents,
)
from .netlist_model import ConeTooWide, UnsupportedCell

__all__ = [
    "MIN_CHAIN_LENGTH",
    "MAX_PERIOD_CHECK_BITS",
    "StageUpdate",
    "register_updates",
    "find_shift_structures",
    "lfsr_period",
    "berlekamp_massey",
]

#: Below this many stages a "chain" is not evidence of anything -- three
#: registers in a row are a synchroniser, not a stream cipher.
MIN_CHAIN_LENGTH = 4

#: Widest register whose period this pass will confirm by iterating the
#: recurrence.  ``2**24`` steps is a second or two; beyond that the period is
#: left unreported rather than guessed from primitivity tables.
MAX_PERIOD_CHECK_BITS = 24


class StageUpdate(object):
    """One register's next-state function, reduced to its real dependencies."""

    __slots__ = ("ff", "table", "register_deps", "other_deps", "problem")

    def __init__(self, ff, table=None, register_deps=(), other_deps=(), problem=None):
        self.ff = ff
        #: :class:`~hal_crypto.boolfunc.TruthTable` over the dependency net keys.
        self.table = table
        #: net keys of the ``q`` outputs this update reads.
        self.register_deps = tuple(register_deps)
        #: net keys of inputs / undriven nets this update reads.
        self.other_deps = tuple(other_deps)
        #: why the update could not be modelled, when it could not.
        self.problem = problem

    @property
    def name(self):
        return self.ff.name

    @property
    def is_pure_register_function(self):
        return self.problem is None and not self.other_deps and bool(self.register_deps)

    def affine_form(self):
        """``(constant, [net keys])`` when the update is affine, else ``None``.

        Affine over *everything* it reads, registers and external inputs alike:
        a CRC stage is ``crc[i-1] ^ crc[7] ^ din`` and refusing to look at it
        because ``din`` is not a register would lose the whole structure.
        """
        if self.problem is not None or self.table is None:
            return None
        return self.table.linear_terms()

    def single_register_link(self):
        """``(source key, inverted)`` when the update is ``q_src`` or ``!q_src``."""
        if not self.is_pure_register_function or len(self.register_deps) != 1:
            return None
        affine = self.affine_form()
        if affine is None or len(affine[1]) != 1:
            return None
        return self.register_deps[0], affine[0]

    def as_dict(self):
        entry = {
            "register": self.ff.name,
            "register_inputs": list(self.register_deps),
            "other_inputs": list(self.other_deps),
        }
        if self.problem:
            entry["problem"] = self.problem
        elif self.table is not None:
            entry["anf"] = boolfunc.anf_string(self.table)
        return entry


# ---------------------------------------------------------------------------
# register graph
# ---------------------------------------------------------------------------


def register_updates(model):
    """The next-state function of every ``tennm_ff``, keyed by instance name."""
    updates = {}
    for ff in model.ff_instances:
        problem = model.ff_configuration_problem(ff)
        if problem is not None:
            updates[ff.name] = StageUpdate(ff, problem=problem)
            continue
        data = model.ff_pin(ff, "d")
        if data[0] == "const":
            updates[ff.name] = StageUpdate(
                ff, problem="the data input is tied to a constant"
            )
            continue
        try:
            table = model.cone_of_bit(ff.connections["d"][0]).restricted()
        except (UnsupportedCell, ConeTooWide) as exc:
            updates[ff.name] = StageUpdate(ff, problem=str(exc))
            continue
        register_deps = []
        other_deps = []
        for key in table.inputs:
            (register_deps if model.register_of(key) else other_deps).append(key)
        updates[ff.name] = StageUpdate(
            ff, table=table, register_deps=register_deps, other_deps=other_deps
        )
    return updates


def _q_key(ff):
    bits = ff.connections.get("q")
    if not bits:
        return None
    return getattr(bits[0], "key", None)


def _clock_group(model, ff):
    """``(clk, ena, clrn)`` as comparable strings, for grouping registers."""
    labels = []
    for pin in ("clk", "ena", "clrn"):
        resolved = model.ff_pin(ff, pin)
        if resolved[0] == "const":
            labels.append("const:{}".format(resolved[1]))
        else:
            labels.append("{}{}".format("!" if resolved[2] else "", resolved[1]))
    return tuple(labels)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def find_shift_structures(model):
    """Every shift chain in *model*, with its feedback classified.

    Returns a list of dicts.  ``kind`` is one of ``lfsr``, ``nlfsr`` or
    ``shift_register``; the last one is the honest outcome for a chain that is
    not closed by a feedback function, and it is reported, not dropped.
    """
    updates = register_updates(model)
    by_q = {}
    q_of_name = {}
    for ff in model.ff_instances:
        key = _q_key(ff)
        q_of_name[ff.name] = key
        if key is not None:
            by_q[key] = ff

    results = []
    used = set()

    # Galois first: its stages are not plain shift links, so the Fibonacci walk
    # below would otherwise cut the same register bank into useless fragments.
    for structure in _galois_structures(model, updates, by_q, q_of_name):
        if set(structure["stages"]) & used:
            continue
        used.update(structure["stages"])
        results.append(structure)

    links = {}
    inverted = {}
    for name, update in updates.items():
        link = update.single_register_link()
        if link is None:
            continue
        source = by_q.get(link[0])
        if source is None or source.name == name:
            continue
        links[name] = source.name
        inverted[name] = link[1]

    successors = {}
    for name, source in links.items():
        successors.setdefault(source, []).append(name)
    for value in successors.values():
        value.sort()

    # Two sweeps: open chains have a head that nothing shifts into; closed ones
    # do not, so their head is found as "the stage that is not a shift link".
    for closed in (False, True):
        for start in sorted(updates):
            if start in used or updates[start].problem is not None:
                continue
            if closed:
                if updates[start].single_register_link() is not None:
                    continue
                if not updates[start].register_deps:
                    continue
            elif start in links:
                continue
            chain, ambiguity = _walk(start, successors)
            if len(chain) < MIN_CHAIN_LENGTH:
                continue
            used.update(chain)
            results.append(
                _describe(model, updates, q_of_name, chain, inverted, ambiguity)
            )
    return results


def _walk(start, successors):
    chain = [start]
    seen = {start}
    ambiguity = None
    current = start
    while True:
        following = successors.get(current, [])
        if len(following) > 1:
            ambiguity = (
                "stage {} shifts into {} registers; the chain is not a line, so it "
                "was cut here".format(current, len(following))
            )
            break
        if not following:
            break
        nxt = following[0]
        if nxt in seen:
            break
        chain.append(nxt)
        seen.add(nxt)
        current = nxt
    return chain, ambiguity


# ---------------------------------------------------------------------------
# Fibonacci / open chains
# ---------------------------------------------------------------------------


def _base_result(model, updates, chain, gauge, ambiguity):
    clocking = sorted({_clock_group(model, updates[name].ff) for name in chain})
    return {
        "stages": list(chain),
        "length": len(chain),
        "gauge": list(gauge),
        "clocking": [list(entry) for entry in clocking],
        "uniform_clocking": len(clocking) == 1,
        "ambiguity": ambiguity,
        "head": chain[0],
        "tail": chain[-1],
    }


def _describe(model, updates, q_of_name, chain, inverted, ambiguity):
    length = len(chain)
    position = {name: index for index, name in enumerate(chain)}
    stage_of_key = {
        q_of_name[name]: position[name] for name in chain if q_of_name[name]
    }

    gauge = [0] * length
    for index in range(1, length):
        gauge[index] = gauge[index - 1] ^ inverted.get(chain[index], 0)

    result = _base_result(model, updates, chain, gauge, ambiguity)
    head_update = updates[chain[0]]

    if head_update.problem is not None or not head_update.register_deps:
        result["kind"] = "shift_register"
        result["reason"] = (
            "the first stage is not driven by any register in the chain, so the "
            "chain is open: it shifts data through, it does not feed back"
        )
        return result
    table = head_update.table
    data_inputs = list(head_update.other_deps)
    if data_inputs:
        if not _enters_linearly(table, data_inputs):
            result["kind"] = "shift_register"
            result["reason"] = (
                "the first stage mixes {} from outside the register bank into a "
                "nonlinear term, so the chain has no separable feedback "
                "function".format(", ".join(data_inputs))
            )
            return result
        table = _cofactor_zero(table, data_inputs)
        if table.arity == 0:
            result["kind"] = "shift_register"
            result["reason"] = (
                "the first stage is driven only by {}, which is outside the register "
                "bank: the chain is open".format(", ".join(data_inputs))
            )
            return result
    result["data_inputs"] = data_inputs

    taps = []
    for key in table.inputs:
        stage = stage_of_key.get(key)
        if stage is None:
            result["kind"] = "shift_register"
            result["reason"] = (
                "the first stage reads register {}, which is not a stage of this "
                "chain".format(key)
            )
            return result
        taps.append(stage)

    logical = _logical_feedback(table, taps, gauge)
    sorted_taps = sorted(taps)
    result["taps"] = sorted_taps
    result["form"] = "fibonacci"
    result["form_reason"] = (
        "exactly one stage is driven by a function of several stages; every other "
        "stage takes its value from the one before it"
    )
    result["autonomous"] = not data_inputs
    result["feedback_reads_last_stage"] = (length - 1) in sorted_taps

    affine = logical.linear_terms()
    if affine is None:
        result["kind"] = "nlfsr"
        result["state_encoding"] = "as-stored"
        result["feedback_anf"] = boolfunc.anf_string(logical)
        result["feedback_degree"] = logical.algebraic_degree()
        result["nonlinear_terms"] = [list(term) for term in logical.anf() if len(term) >= 2]
        return result

    constant = affine[0]
    encoding = "as-stored"
    if constant and len(sorted_taps) % 2 == 0:
        logical = _complement_all(logical)
        constant = logical.linear_terms()[0]
        encoding = "complemented"
    result["kind"] = "lfsr"
    result["feedback_constant"] = constant
    result["state_encoding"] = encoding
    result["feedback_anf"] = boolfunc.anf_string(logical)
    _attach_polynomial(result, sorted_taps)
    if constant == 0 and result["feedback_reads_last_stage"] and length <= MAX_PERIOD_CHECK_BITS:
        period = lfsr_period(length, sorted_taps)
        result["period"] = period
        result["maximal_length"] = period == (1 << length) - 1
    return result


def _logical_feedback(table, taps, gauge):
    """Re-index *table* to tap order and rewrite it over the gauge variables."""
    order = sorted(range(len(taps)), key=lambda index: taps[index])
    names = tuple("s[{}]".format(taps[index]) for index in order)
    values = [
        table.values[_permute_address(index, order)] for index in range(len(table.values))
    ]
    permuted = boolfunc.TruthTable(names, values)
    complement_mask = 0
    for slot, index in enumerate(order):
        if gauge[taps[index]]:
            complement_mask |= 1 << slot
    size = len(permuted.values)
    return boolfunc.TruthTable(
        names, [permuted.values[index ^ complement_mask] for index in range(size)]
    )


def _attach_polynomial(result, taps):
    """Record the feedback polynomial under both index conventions.

    The pass numbers stage 0 as the chain head, so a tap at stage *t* is
    ``x^(t+1)``; that is the convention ``examples/agilex3_walkthroughs/05``
    states for its LFSR.  Numbering the register from the other end -- which is
    what a CRC specification does -- gives the reciprocal, and the same silicon
    is then written with a different polynomial.  Reporting only one of the two
    would make the finding wrong for half the designs, so both are recorded and
    the convention is named.
    """
    exponents = polynomial_exponents(taps)
    result["polynomial"] = polynomial_from_exponents(exponents)
    result["polynomial_exponents"] = exponents
    reciprocal = reciprocal_exponents(exponents)
    result["polynomial_reciprocal"] = polynomial_from_exponents(reciprocal)
    result["polynomial_reciprocal_exponents"] = reciprocal
    result["polynomial_convention"] = (
        "stage 0 is the chain head and a tap at stage t contributes x^(t+1); "
        "numbering the register from the other end gives the reciprocal"
    )


def _permute_address(index, order):
    address = 0
    for slot, source in enumerate(order):
        if (index >> slot) & 1:
            address |= 1 << source
    return address


def _complement_all(table):
    """``f`` rewritten for complemented inputs *and* a complemented output."""
    full = len(table.values) - 1
    return boolfunc.TruthTable(
        table.inputs, [1 - table.values[index ^ full] for index in range(full + 1)]
    )


def _enters_linearly(table, names):
    """True when every ANF term touching *names* is that name on its own.

    ``f = g(state) ^ din`` keeps the feedback separable, so the register part
    can be read out by setting ``din`` to zero.  ``f = g(state) & din`` does
    not, and the pass has to say so instead of quietly dropping the input.
    """
    wanted = set(names)
    for term in table.anf():
        touched = wanted & set(term)
        if touched and len(term) != 1:
            return False
    return True


def _cofactor_zero(table, names):
    """The function with every input in *names* fixed to zero."""
    keep = [
        position for position, name in enumerate(table.inputs) if name not in set(names)
    ]
    inputs = tuple(table.inputs[position] for position in keep)
    values = []
    for index in range(1 << len(keep)):
        address = 0
        for slot, position in enumerate(keep):
            address |= ((index >> slot) & 1) << position
        values.append(table.values[address])
    return boolfunc.TruthTable(inputs, values)


# ---------------------------------------------------------------------------
# Galois form
# ---------------------------------------------------------------------------


def _galois_structures(model, updates, by_q, q_of_name):
    """Chains where the last stage is XORed into several stages along the way.

    A single external net XORed into the same stages is allowed and recorded as
    ``data_inputs``: that is a CRC or a data-absorbing scrambler, which has the
    same feedback polynomial as the autonomous register but is not an
    autonomous generator.  The caller is told which it got.
    """
    results = []
    claimed = set()
    externals = set()
    for update in updates.values():
        affine = update.affine_form()
        if affine is not None:
            externals.update(update.other_deps)
    data_options = [None] + sorted(externals)[:4]
    for tail_name, data_key in [
        (name, data) for name in sorted(updates) for data in data_options
    ]:
        if tail_name in claimed:
            continue
        tail_key = q_of_name.get(tail_name)
        if tail_key is None:
            continue
        predecessor = {}
        for name in sorted(updates):
            update = updates[name]
            affine = update.affine_form()
            if affine is None:
                continue
            outside = set(update.other_deps)
            if outside - ({data_key} if data_key else set()):
                continue
            deps = list(update.register_deps)
            if len(deps) == 1 and deps[0] != q_of_name.get(name):
                predecessor[name] = (deps[0], False, affine[0])
            elif len(deps) == 2 and tail_key in deps:
                other = deps[0] if deps[1] == tail_key else deps[1]
                if other == q_of_name.get(name):
                    continue
                predecessor[name] = (other, True, affine[0])

        heads = [
            name
            for name, entry in predecessor.items()
            if entry[0] == tail_key and not entry[1] and name != tail_name
        ]
        if len(heads) != 1:
            continue
        successors = {}
        for name, entry in predecessor.items():
            source = by_q.get(entry[0])
            if source is not None:
                successors.setdefault(source.name, []).append(name)
        chain = [heads[0]]
        seen = set(chain)
        current = heads[0]
        broken = False
        while current != tail_name:
            following = [name for name in sorted(successors.get(current, [])) if name not in seen]
            if len(following) != 1:
                broken = True
                break
            current = following[0]
            chain.append(current)
            seen.add(current)
        if broken or len(chain) < MIN_CHAIN_LENGTH:
            continue
        injections = [index for index, name in enumerate(chain) if predecessor[name][1]]
        if not injections:
            continue  # a plain circular shift register, not an LFSR

        gauge = [0] * len(chain)
        for index in range(1, len(chain)):
            gauge[index] = gauge[index - 1] ^ predecessor[chain[index]][2]
        used_data = sorted(
            {key for name in chain for key in updates[name].other_deps}
        )
        result = _base_result(model, updates, chain, gauge, None)
        result["kind"] = "lfsr"
        result["form"] = "galois"
        result["data_inputs"] = used_data
        result["autonomous"] = not used_data
        result["form_reason"] = (
            "the last stage is XORed into {} stage(s) along the chain instead of a "
            "single feedback function driving the head".format(len(injections))
        )
        result["injection_stages"] = injections
        result["feedback_constant"] = sum(
            predecessor[name][2] for name in chain
        ) % 2
        result["state_encoding"] = "as-stored"
        polynomial = _galois_polynomial(len(chain), injections)
        result["taps"] = polynomial
        _attach_polynomial(result, polynomial)
        result["feedback_reads_last_stage"] = (len(chain) - 1) in polynomial
        if result["feedback_constant"] == 0 and len(chain) <= MAX_PERIOD_CHECK_BITS:
            period = lfsr_period(len(chain), polynomial)
            result["period"] = period
            result["maximal_length"] = period == (1 << len(chain)) - 1
        result["feedback_anf"] = " ^ ".join("s[{}]".format(tap) for tap in polynomial)
        results.append(result)
        claimed.update(chain)
    return results


def _galois_polynomial(length, injections):
    """The Fibonacci tap set with the same characteristic polynomial.

    Derived, not assumed: the Galois state map is simulated, the resulting bit
    sequence is run through Berlekamp-Massey, and the minimal polynomial that
    comes back is converted to tap positions.  If the sequence turns out to be
    degenerate (a minimal polynomial shorter than the register), the injection
    positions are returned unchanged and the caller can see the length mismatch.
    """
    mask = (1 << length) - 1
    injection_mask = 0
    for index in injections:
        injection_mask |= 1 << index
    state = 1
    sequence = []
    for _ in range(2 * length + 2):
        sequence.append(state & 1)
        feedback = (state >> (length - 1)) & 1
        state = (state << 1) & mask
        if feedback:
            state ^= 1 | injection_mask
    coefficients = berlekamp_massey(sequence)
    degree = len(coefficients) - 1
    if degree != length:
        return sorted({length - 1} | {length - 1 - index for index in injections if index})
    return sorted(index - 1 for index in range(1, degree + 1) if coefficients[index])


def berlekamp_massey(sequence):
    """Minimal LFSR connection polynomial of a GF(2) sequence.

    Returns the coefficients ``c`` of ``C(x) = 1 + c1 x + ... + cL x^L`` as a
    list with ``c[0] == 1``.
    """
    length = len(sequence)
    current = [1]
    previous = [1]
    degree = 0
    last = -1
    for position in range(length):
        discrepancy = sequence[position]
        for index in range(1, degree + 1):
            if index < len(current) and current[index]:
                discrepancy ^= sequence[position - index]
        if not discrepancy:
            continue
        shift = position - last
        updated = list(current)
        if len(updated) < len(previous) + shift:
            updated.extend([0] * (len(previous) + shift - len(updated)))
        for index, coefficient in enumerate(previous):
            if coefficient:
                updated[index + shift] ^= 1
        if 2 * degree <= position:
            previous = current
            degree = position + 1 - degree
            last = position
        current = updated
    while len(current) > degree + 1:
        current.pop()
    while len(current) < degree + 1:
        current.append(0)
    return current


def lfsr_period(length, taps):
    """Period of the Fibonacci recurrence with 0-based ``taps``, from state 1.

    A property of the extracted polynomial, computed by iterating it -- not a
    claim about the netlist beyond the polynomial the pass extracted.
    """
    if length > MAX_PERIOD_CHECK_BITS:
        raise ValueError("period check refused above {} bits".format(MAX_PERIOD_CHECK_BITS))
    mask = (1 << length) - 1
    tap_mask = 0
    for tap in taps:
        tap_mask |= 1 << tap
    state = 1
    steps = 0
    limit = 1 << length
    while True:
        feedback = bin(state & tap_mask).count("1") & 1
        state = ((state << 1) & mask) | feedback
        steps += 1
        if state == 1:
            return steps
        if steps > limit:
            return None
