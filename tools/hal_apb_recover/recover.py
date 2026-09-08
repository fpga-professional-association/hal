"""Recover an APB register map from a flattened netlist.

The whole analysis is a family of *probes*.  A probe pins the APB bus to one
access (address, direction, data pattern, strobes), pins a chosen storage bit
and a chosen data bit, leaves everything else in one of three environments, and
reads back the next state of the flip-flops and the value on PRDATA.  The three
environments are the reason the output can distinguish what it knows:

``abstract``
    every other primary input, every other state bit and every other data bit
    is left unconstrained.  A definite answer here holds for *all* of them, so
    the claim needs no environment assumption beyond the bus mapping itself --
    ``proven_under_assumptions``.
``quiescent_low`` / ``quiescent_high``
    the non-APB inputs are pinned to the value the mapping declares quiescent,
    and the other state and data bits are pinned to 0 and to 1.  Agreement of
    both is a check of one transfer in two concrete environments, not a proof
    -- ``proven_bounded`` with a cycle bound of one transfer.

If only one environment gives a definite answer the field is ``heuristic`` and
the analysis looks for the state bit that guards it; if none does, the field is
reported ``unknown`` and stays in ``unresolved_fields``.  Nothing is filled in
by guessing.
"""

import time

from . import REGISTER_MAP_SCHEMA, REGISTER_MAP_SCHEMA_VERSION, __version__

__all__ = [
    "RecoveryOptions",
    "RecoveryError",
    "recover",
    "TEMPLATES",
    "TIER_PROVEN",
    "TIER_BOUNDED",
    "TIER_HEURISTIC",
    "TIER_UNKNOWN",
    "SIDE_EFFECT_TEMPLATES",
]

TIER_PROVEN = "proven_under_assumptions"
TIER_BOUNDED = "proven_bounded"
TIER_HEURISTIC = "heuristic"
TIER_UNKNOWN = "unknown"

CONTEXT_ABSTRACT = "abstract"
CONTEXT_LOW = "quiescent_low"
CONTEXT_HIGH = "quiescent_high"
CONTEXTS = (CONTEXT_ABSTRACT, CONTEXT_LOW, CONTEXT_HIGH)

#: next-state templates, keyed by ``(data_bit, state_bit)``.
TEMPLATES = {
    "write": lambda d, s: d,
    "write_inverted": lambda d, s: 1 - d,
    "hold": lambda d, s: s,
    "write_one_to_clear": lambda d, s: s & (1 - d),
    "write_one_to_set": lambda d, s: s | d,
    "write_zero_to_clear": lambda d, s: s & d,
    "clear_on_write": lambda d, s: 0,
    "set_on_write": lambda d, s: 1,
}

#: templates that change the bit without storing it -- the side-effect writes
SIDE_EFFECT_TEMPLATES = (
    "write_one_to_clear",
    "write_one_to_set",
    "write_zero_to_clear",
    "clear_on_write",
    "set_on_write",
)

_ACCESS_BY_TEMPLATE = {
    "write": "read-write",
    "write_inverted": "read-write",
    "hold": "read-only",
    "write_one_to_clear": "read-write-one-to-clear",
    "write_one_to_set": "read-write-one-to-set",
    "write_zero_to_clear": "read-write-zero-to-clear",
    "clear_on_write": "read-clear-on-write",
    "set_on_write": "read-set-on-write",
}


class RecoveryError(RuntimeError):
    """The recovery cannot run at all (not: the recovery found nothing)."""


class RecoveryOptions(object):
    def __init__(self, guard_scan_limit=128, read_scan=True, tag=None):
        self.guard_scan_limit = int(guard_scan_limit)
        self.read_scan = bool(read_scan)
        self.tag = tag


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------


class _Prober(object):
    def __init__(self, circuit, mapping):
        self.circuit = circuit
        self.mapping = mapping
        self.elements = circuit.state_elements
        self.bus_nets = mapping.bus_nets()
        self.other_inputs = [net for net in circuit.input_nets if net not in self.bus_nets]
        self.probe_count = 0

    @staticmethod
    def _fill(context):
        if context == CONTEXT_ABSTRACT:
            return None
        return 0 if context == CONTEXT_LOW else 1

    def assignment(
        self,
        context,
        address=None,
        pwrite=1,
        psel=1,
        penable=1,
        pwdata=None,
        pstrb=None,
        state=None,
        paddr=None,
    ):
        mapping = self.mapping
        fill = self._fill(context)
        values = {}
        if context != CONTEXT_ABSTRACT and mapping.quiescent_value is not None:
            for net in self.other_inputs:
                values[net] = mapping.quiescent_value
        if mapping.reset_net is not None:
            values[mapping.reset_net] = 1 if mapping.reset_active == "low" else 0
        values[mapping.scalars["psel"]] = psel
        values[mapping.scalars["penable"]] = penable
        values[mapping.scalars["pwrite"]] = pwrite

        address_bits = dict(paddr or {})
        if address is not None:
            for index, net in enumerate(mapping.vectors["paddr"]):
                address_bits.setdefault(index, (address >> index) & 1)
        for index, net in enumerate(mapping.vectors["paddr"]):
            bit = address_bits.get(index)
            if bit is not None:
                values[net] = bit

        for index, net in enumerate(mapping.vectors["pwdata"]):
            value = (pwdata or {}).get(index, fill)
            if value is not None:
                values[net] = value
        for index, net in enumerate(mapping.vectors["pstrb"]):
            value = (pstrb or {}).get(index, 1)
            if value is not None:
                values[net] = value

        state_bits = {}
        for element in self.elements:
            value = (state or {}).get(element.key, fill)
            if value is not None:
                state_bits[element.key] = value
        values.update(self.circuit.state_vector_nets(state_bits))
        return values

    def evaluate(self, context, **kwargs):
        self.probe_count += 1
        return self.circuit.evaluate(self.assignment(context, **kwargs))

    def next_state(self, values, elements=None):
        return self.circuit.next_state(values, elements)

    def read_data(self, values):
        return [values.get(net) for net in self.mapping.vectors["prdata"]]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _match_template(table):
    """``table`` maps ``(d, s)`` to a next value; return the template name."""
    if any(value is None for value in table.values()):
        return None
    for name, function in TEMPLATES.items():
        if all(table[(d, s)] == function(d, s) for d in (0, 1) for s in (0, 1)):
            return name
    return None


def _table_json(table):
    return {
        "d{}s{}".format(d, s): ("x" if table[(d, s)] is None else table[(d, s)])
        for d in (0, 1)
        for s in (0, 1)
    }


def _hex(value, bits):
    width = max(1, (bits + 3) // 4)
    return "0x{:0{}x}".format(value, width)


# ---------------------------------------------------------------------------
# the recovery
# ---------------------------------------------------------------------------


def recover(circuit, mapping, options=None):
    """Run the full recovery and return a register-map document."""
    options = options or RecoveryOptions()
    started = time.time()
    prober = _Prober(circuit, mapping)

    if not circuit.state_elements:
        raise RecoveryError(
            "the netlist has no modelled flip-flops, so there is no register state to "
            "recover; check the gate library and the netlist"
        )

    reset_values, reset_notes = _recover_reset_values(prober)
    clock_report = _check_clocks(prober)
    read_map = _recover_read_mux(prober) if options.read_scan else {}
    write_map, unresolved = _recover_writes(prober, options)

    registers, unmapped = _assemble_registers(prober, read_map, write_map, reset_values)
    alias_classes = _detect_aliases(registers)
    dead_address_bits = _find_undecoded_address_bits(prober, registers)

    document = {
        "schema": REGISTER_MAP_SCHEMA,
        "schema_version": REGISTER_MAP_SCHEMA_VERSION,
        "design": mapping.design or circuit.name,
        "generated_by": {
            "tool": "hal_apb_recover",
            "version": __version__,
            "tag": options.tag,
        },
        "netlist": {
            "name": circuit.name,
            "source": circuit.source,
            "gate_library": circuit.gate_library,
            "statistics": circuit.statistics(),
        },
        "mapping": mapping.to_json(),
        "assumptions": [
            {"id": identifier, "kind": kind, "description": description}
            for identifier, kind, description in mapping.assumption_records()
        ],
        "storage": {
            element.key: {
                "uid": element.gate.uid,
                "name": element.gate.name,
                "type": element.gate.type_name,
            }
            for element in circuit.state_elements
        },
        "registers": registers,
        "unmapped_addresses": unmapped,
        "alias_classes": alias_classes,
        "unresolved_fields": unresolved,
        "coverage": _coverage(prober, circuit, mapping, registers, dead_address_bits,
                              clock_report, reset_notes),
        "metrics": {
            "probes": prober.probe_count,
            "duration_s": round(time.time() - started, 3),
        },
    }
    return document


# -- reset ------------------------------------------------------------------


def _recover_reset_values(prober):
    """Read every flip-flop's asynchronous reset value with reset asserted."""
    mapping = prober.mapping
    notes = []
    if mapping.reset_net is None:
        return {}, [
            "the mapping declares no reset net, so no reset values were recovered"
        ]

    values = prober.assignment(CONTEXT_ABSTRACT)
    values[mapping.reset_net] = 0 if mapping.reset_active == "low" else 1
    propagated = prober.circuit.evaluate(values)
    prober.probe_count += 1
    verdicts = prober.circuit.asynchronous_values(propagated)

    reset_values = {}
    without_async = []
    for key, verdict in sorted(verdicts.items()):
        if verdict in (0, 1):
            reset_values[key] = verdict
        elif verdict == "none":
            without_async.append(key)
    if without_async:
        notes.append(
            "{} flip-flop(s) have no asynchronous reset that {} drives; their reset "
            "value is reported as unknown rather than assumed to be 0 ({}{})".format(
                len(without_async),
                prober.circuit.net_name(mapping.reset_net),
                ", ".join(without_async[:8]),
                ", ..." if len(without_async) > 8 else "",
            )
        )
    return reset_values, notes


def _check_clocks(prober):
    """Which flip-flops are *not* clocked by the declared clock net."""
    mapping = prober.mapping
    if mapping.clock_net is None:
        return {"checked": False, "foreign": [], "unknown": []}
    foreign, unknown = [], []
    for element in prober.circuit.state_elements:
        net = prober.circuit.clock_net(element)
        if net is None:
            unknown.append(element.key)
        elif net != mapping.clock_net:
            foreign.append({"storage": element.key, "clock": prober.circuit.net_name(net)})
    return {"checked": True, "foreign": foreign, "unknown": unknown}


# -- read mux ---------------------------------------------------------------


def _recover_read_mux(prober):
    """Associate every PRDATA bit with a state bit or a constant, per address."""
    mapping = prober.mapping
    elements = prober.circuit.state_elements
    result = {}

    for address in mapping.addresses():
        abstract = prober.read_data(
            prober.evaluate(CONTEXT_ABSTRACT, address=address, pwrite=0)
        )
        low = prober.read_data(prober.evaluate(CONTEXT_LOW, address=address, pwrite=0))
        high = prober.read_data(prober.evaluate(CONTEXT_HIGH, address=address, pwrite=0))

        bits = {}
        undecided = []
        for index in range(mapping.read_width):
            if abstract[index] is not None:
                bits[index] = {
                    "source": "constant",
                    "value": abstract[index],
                    "confidence": TIER_PROVEN,
                }
            elif low[index] is not None and low[index] == high[index]:
                bits[index] = {
                    "source": "constant",
                    "value": low[index],
                    "confidence": TIER_BOUNDED,
                }
            else:
                undecided.append(index)

        if undecided:
            candidates = {index: [] for index in undecided}
            for element in elements:
                probed = prober.read_data(
                    prober.evaluate(
                        CONTEXT_LOW, address=address, pwrite=0, state={element.key: 1}
                    )
                )
                for index in undecided:
                    if probed[index] != low[index]:
                        candidates[index].append(element.key)
            for index in undecided:
                names = candidates[index]
                if len(names) == 1:
                    bits[index] = _characterise_read_bit(prober, address, index, names[0])
                elif names:
                    bits[index] = {
                        "source": "ambiguous",
                        "candidates": sorted(names),
                        "confidence": TIER_UNKNOWN,
                        "reason": "{} state bits influence this PRDATA bit; the read "
                        "mux could not be resolved to a single storage bit".format(len(names)),
                    }
                else:
                    bits[index] = {
                        "source": "unresolved",
                        "confidence": TIER_UNKNOWN,
                        "reason": "PRDATA bit is not constant and no single state bit "
                        "changes it; it depends on inputs outside the mapped bus",
                    }
        result[address] = bits
    return result


def _characterise_read_bit(prober, address, index, storage):
    """Decide the tier and polarity of one PRDATA-bit <- state-bit association."""
    outcomes = {}
    for context in CONTEXTS:
        low = prober.read_data(
            prober.evaluate(context, address=address, pwrite=0, state={storage: 0})
        )[index]
        high = prober.read_data(
            prober.evaluate(context, address=address, pwrite=0, state={storage: 1})
        )[index]
        if low == 0 and high == 1:
            outcomes[context] = "direct"
        elif low == 1 and high == 0:
            outcomes[context] = "inverted"
        else:
            outcomes[context] = None

    if outcomes[CONTEXT_ABSTRACT] is not None:
        confidence, polarity = TIER_PROVEN, outcomes[CONTEXT_ABSTRACT]
    elif (
        outcomes[CONTEXT_LOW] is not None
        and outcomes[CONTEXT_LOW] == outcomes[CONTEXT_HIGH]
    ):
        confidence, polarity = TIER_BOUNDED, outcomes[CONTEXT_LOW]
    elif outcomes[CONTEXT_LOW] is not None or outcomes[CONTEXT_HIGH] is not None:
        confidence = TIER_HEURISTIC
        polarity = outcomes[CONTEXT_LOW] or outcomes[CONTEXT_HIGH]
    else:
        return {
            "source": "unresolved",
            "confidence": TIER_UNKNOWN,
            "reason": "state bit {} changes this PRDATA bit but not monotonically in "
            "any probed environment".format(storage),
        }

    return {
        "source": "storage",
        "storage": storage,
        "polarity": polarity,
        "confidence": confidence,
        "environments": {name: outcomes[name] for name in CONTEXTS},
    }


# -- writes -----------------------------------------------------------------


def _recover_writes(prober, options):
    """Find, per address, which storage bit follows which PWDATA bit, and how."""
    mapping = prober.mapping
    write_map = {}
    unresolved = []

    for address in mapping.addresses():
        candidates = _write_candidates(prober, address)
        fields = {}
        for storage, data_bit in sorted(candidates):
            field = _characterise_field(prober, options, address, storage, data_bit)
            if field["confidence"] == TIER_UNKNOWN:
                unresolved.append(
                    {
                        "address": address,
                        "address_hex": _hex(address, mapping.address_width),
                        "storage": storage,
                        "data_bit": data_bit,
                        "reason": field.get("reason", "no consistent next-state template"),
                        "environments": field.get("environments"),
                    }
                )
                continue
            if field["template"] == "hold":
                continue
            fields[(storage, data_bit)] = field
        write_map[address] = fields
    return write_map, unresolved


def _write_candidates(prober, address):
    """Storage/data-bit pairs that a write at ``address`` plausibly touches."""
    mapping = prober.mapping
    elements = prober.circuit.state_elements
    candidates = set()

    for context, state_fill in ((CONTEXT_LOW, 0), (CONTEXT_HIGH, 1)):
        baseline = prober.next_state(
            prober.evaluate(context, address=address, pwrite=1)
        )
        for data_bit in range(mapping.data_width):
            probed = prober.next_state(
                prober.evaluate(
                    context,
                    address=address,
                    pwrite=1,
                    pwdata={data_bit: 1 - state_fill},
                )
            )
            for element in elements:
                if probed[element.key] != baseline[element.key]:
                    candidates.add((element.key, data_bit))
    return candidates


def _field_table(prober, context, address, storage, data_bit, extra_state=None,
                 pstrb=None, psel=1, penable=1, pwrite=1, pwdata_overrides=None):
    table = {}
    for d in (0, 1):
        for s in (0, 1):
            state = {storage: s}
            if extra_state:
                state.update(extra_state)
            pwdata = dict(pwdata_overrides or {})
            pwdata[data_bit] = d
            values = prober.evaluate(
                context,
                address=address,
                pwrite=pwrite,
                psel=psel,
                penable=penable,
                pwdata=pwdata,
                pstrb=pstrb,
                state=state,
            )
            element = prober.circuit.state_by_key[storage]
            table[(d, s)] = prober.next_state(values, [element])[storage]
    return table


def _characterise_field(prober, options, address, storage, data_bit):
    tables = {}
    templates = {}
    for context in CONTEXTS:
        table = _field_table(prober, context, address, storage, data_bit)
        tables[context] = table
        templates[context] = _match_template(table)

    def unresolved(reason, extra=None):
        record = {
            "storage": storage,
            "data_bit": data_bit,
            "confidence": TIER_UNKNOWN,
            "template": None,
            "reason": reason,
            "environments": {name: templates[name] for name in CONTEXTS},
            "next_state_tables": {name: _table_json(tables[name]) for name in CONTEXTS},
        }
        record.update(extra or {})
        return record

    guards = None
    if templates[CONTEXT_ABSTRACT] is not None:
        confidence, chosen = TIER_PROVEN, CONTEXT_ABSTRACT
    elif (
        templates[CONTEXT_LOW] is not None
        and templates[CONTEXT_LOW] == templates[CONTEXT_HIGH]
    ):
        confidence, chosen = TIER_BOUNDED, CONTEXT_LOW
    elif templates[CONTEXT_LOW] is not None or templates[CONTEXT_HIGH] is not None:
        definite = [
            context
            for context in (CONTEXT_LOW, CONTEXT_HIGH)
            if templates[context] is not None
        ]
        # A register written in one environment and merely holding in the other
        # is a *guarded* write, not a read-only bit; report the write and name
        # the guard rather than the more convenient answer.  But if nothing in
        # the state explains the disagreement, the field is not describable by a
        # single-data-bit access rule at all, and saying so is the honest answer.
        writing = [context for context in definite if templates[context] != "hold"]
        chosen = (writing or definite)[0]
        guards = _scan_guards(prober, options, address, storage, data_bit, templates, chosen)
        if guards is None:
            dependencies = _scan_data_dependencies(
                prober, options, address, storage, data_bit, templates, chosen
            )
            if dependencies:
                return unresolved(
                    "the next-state rule changes with PWDATA bit(s) {}, so no "
                    "single-data-bit access template describes this field".format(
                        ", ".join(str(entry["data_bit"]) for entry in dependencies)
                    ),
                    {"data_dependencies": dependencies},
                )
            return unresolved(
                "the probed environments disagree ({}) and neither a state bit nor "
                "another data bit explains the difference".format(
                    ", ".join(
                        "{}={}".format(name, templates[name]) for name in (CONTEXT_LOW, CONTEXT_HIGH)
                    )
                )
            )
        confidence = TIER_HEURISTIC
    else:
        return unresolved("no environment produced a definite next-state table")
    template = templates[chosen]

    field = {
        "storage": storage,
        "data_bit": data_bit,
        "template": template,
        "access": _ACCESS_BY_TEMPLATE.get(template, "unknown"),
        "confidence": confidence,
        "evidence_environment": chosen,
        "next_state_table": _table_json(tables[chosen]),
        "environments": {name: templates[name] for name in CONTEXTS},
    }
    if guards:
        field["guarded_by"] = guards
    # A guarded field only shows its write in one environment, so qualify it
    # there; qualifying it in both would report "no strobe lane enables this
    # write", which is false.
    contexts = (chosen,) if confidence == TIER_HEURISTIC else (CONTEXT_LOW, CONTEXT_HIGH)
    field.update(_write_qualifiers(prober, address, storage, data_bit, template, contexts))
    return field


def _scan_guards(prober, options, address, storage, data_bit, templates, chosen):
    """Find the state bits that decide which next-state template applies.

    The scan starts in the environment whose template *differs* from the
    reported one and flips one other state bit at a time; a bit that switches
    the behaviour to the reported template is a candidate guard (a lock bit, an
    enable bit, a mode bit).
    """
    other = CONTEXT_HIGH if chosen == CONTEXT_LOW else CONTEXT_LOW
    if templates[other] is None or templates[other] == templates[chosen]:
        return None
    target = templates[chosen]
    guard_value = 1 if other == CONTEXT_LOW else 0
    guards = []
    scanned = 0
    truncated = False
    for element in prober.circuit.state_elements:
        if element.key == storage:
            continue
        if scanned >= options.guard_scan_limit:
            truncated = True
            break
        scanned += 1
        table = _field_table(
            prober,
            other,
            address,
            storage,
            data_bit,
            extra_state={element.key: guard_value},
        )
        if _match_template(table) == target:
            guards.append({"storage": element.key, "enabling_value": guard_value})
            if len(guards) >= 4:
                break
    if not guards and not truncated:
        return None
    return {
        "candidates": guards,
        "truncated": truncated,
        "scanned": scanned,
        "reported_template": target,
        "alternative_template": templates[other],
        "alternative_environment": other,
    }


def _scan_data_dependencies(prober, options, address, storage, data_bit, templates, chosen):
    """Find other PWDATA bits that change which next-state template applies."""
    other = CONTEXT_HIGH if chosen == CONTEXT_LOW else CONTEXT_LOW
    if templates[other] is None or templates[other] == templates[chosen]:
        return []
    target = templates[chosen]
    other_value = 1 if other == CONTEXT_LOW else 0
    found = []
    for index in range(prober.mapping.data_width):
        if index == data_bit:
            continue
        if len(found) >= 4:
            break
        table = _field_table(
            prober,
            other,
            address,
            storage,
            data_bit,
            pwdata_overrides={index: other_value},
        )
        if _match_template(table) == target:
            found.append({"data_bit": index, "value": other_value})
    return found


def _write_qualifiers(prober, address, storage, data_bit, template, contexts):
    """Check which bus conditions the write actually depends on."""
    mapping = prober.mapping
    requires = []
    for label, kwargs in (
        ("psel", {"psel": 0}),
        ("penable", {"penable": 0}),
        ("pwrite", {"pwrite": 0}),
    ):
        held = True
        for context in contexts:
            table = _field_table(
                prober, context, address, storage, data_bit, **kwargs
            )
            if _match_template(table) != "hold":
                held = False
                break
        if held:
            requires.append(label)

    lanes = []
    lanes_checked = []
    if mapping.strobe_lanes:
        for lane in range(mapping.strobe_lanes):
            pstrb = {index: (1 if index == lane else 0) for index in range(mapping.strobe_lanes)}
            active = None
            for context in contexts:
                table = _field_table(
                    prober, context, address, storage, data_bit, pstrb=pstrb
                )
                matched = _match_template(table)
                if matched is None:
                    active = None
                    break
                is_active = matched == template
                if active is None:
                    active = is_active
                elif active != is_active:
                    active = None
                    break
            lanes_checked.append({"lane": lane, "active": active})
            if active:
                lanes.append(lane)

    result = {"write_requires": requires}
    if mapping.strobe_lanes:
        result["strobe_lanes"] = lanes
        result["strobe_lane_probes"] = lanes_checked
        expected = mapping.strobe_lane_of_bit(data_bit)
        if expected is not None and lanes and lanes != [expected]:
            result["strobe_note"] = (
                "data bit {} sits in byte lane {} but the write is enabled by lane(s) "
                "{}".format(data_bit, expected, lanes)
            )
    return result


# -- assembly ---------------------------------------------------------------


def _assemble_registers(prober, read_map, write_map, reset_values):
    mapping = prober.mapping
    registers = []
    unmapped = []

    for address in mapping.addresses():
        read_bits = read_map.get(address, {})
        fields = write_map.get(address, {})

        storage_read = {}
        for index, entry in read_bits.items():
            if entry.get("source") == "storage":
                storage_read[entry["storage"]] = index

        register_fields = []
        for (storage, data_bit), field in sorted(fields.items(), key=lambda item: item[0][1]):
            record = dict(field)
            record["kind"] = "storage"
            record["bit"] = data_bit
            record["read_bit"] = storage_read.get(storage)
            record["reset_value"] = reset_values.get(storage)
            if record["read_bit"] is None:
                record.setdefault("notes", []).append(
                    "this storage bit is written at this address but is not readable "
                    "through PRDATA here (write-only or read at another address)"
                )
            register_fields.append(record)

        written_storage = {storage for storage, _ in fields}
        for storage, index in sorted(storage_read.items(), key=lambda item: item[1]):
            if storage in written_storage:
                continue
            entry = read_bits[index]
            register_fields.append(
                {
                    "kind": "storage",
                    "bit": index,
                    "read_bit": index,
                    "storage": storage,
                    "template": "hold",
                    "access": "read-only",
                    "confidence": entry.get("confidence", TIER_UNKNOWN),
                    "polarity": entry.get("polarity"),
                    "reset_value": reset_values.get(storage),
                    "notes": [
                        "readable at this address but no write at this address changes it"
                    ],
                }
            )

        constant_bits = {
            index: entry["value"]
            for index, entry in read_bits.items()
            if entry.get("source") == "constant"
        }
        storage_bits = {field["bit"] for field in register_fields}
        for index, value in sorted(constant_bits.items()):
            if index in storage_bits:
                continue
            register_fields.append(
                {
                    "kind": "constant",
                    "bit": index,
                    "read_bit": index,
                    "storage": None,
                    "template": "constant",
                    "access": "read-only",
                    "constant_value": value,
                    "reset_value": value,
                    "confidence": read_bits[index].get("confidence", TIER_UNKNOWN),
                    "notes": [
                        "no storage bit reaches this PRDATA bit at this address; it "
                        "reads a constant {}".format(value)
                    ],
                }
            )

        unresolved_read = sorted(
            index
            for index, entry in read_bits.items()
            if entry.get("source") in ("ambiguous", "unresolved")
        )

        non_zero_constants = {
            index: value for index, value in constant_bits.items() if value == 1
        }
        if not fields and not storage_read and not non_zero_constants and not unresolved_read:
            unmapped.append(
                {
                    "address": address,
                    "address_hex": _hex(address, mapping.address_width),
                    "evidence": "no write at this address changes any modelled state "
                    "bit and every PRDATA bit reads 0",
                }
            )
            continue

        register_fields.sort(key=lambda record: record["bit"])
        registers.append(
            {
                "address": address,
                "address_hex": _hex(address, mapping.address_width),
                "name": "REG_{}".format(_hex(address, mapping.address_width)[2:].upper()),
                "access": _register_access(register_fields, constant_bits),
                "fields": register_fields,
                "constant_read_bits": {
                    str(index): value for index, value in sorted(constant_bits.items())
                },
                "unresolved_read_bits": unresolved_read,
                "reset_value": _register_reset(register_fields, constant_bits, mapping),
                "read_value_when_reset": None,
                "confidence": _register_confidence(register_fields),
                "signature": _register_signature(register_fields, constant_bits),
            }
        )

    return registers, unmapped


def _register_access(fields, constant_bits):
    """The register's access, decided by its storage bits.

    Constant read bits are reserved/unimplemented bits; letting them turn a
    write-one-to-clear status register into "mixed" would hide the interesting
    half of the answer, so they only decide the access when there is no storage
    at all.
    """
    storage = {
        field.get("access") for field in fields if field.get("kind") == "storage"
    }
    storage.discard(None)
    if storage:
        return next(iter(storage)) if len(storage) == 1 else "mixed"
    if any(field.get("kind") == "constant" for field in fields) or constant_bits:
        return "read-only"
    return "unknown"


def _register_reset(fields, constant_bits, mapping):
    value = 0
    known = 0
    for field in fields:
        reset = field.get("reset_value")
        if reset is None:
            continue
        known |= 1 << field["bit"]
        if reset:
            value |= 1 << field["bit"]
    width = mapping.data_width
    return {
        "value": _hex(value, width),
        "known_bits": _hex(known, width),
        "complete": known == (1 << width) - 1,
    }


_TIER_ORDER = {TIER_PROVEN: 0, TIER_BOUNDED: 1, TIER_HEURISTIC: 2, TIER_UNKNOWN: 3}


def _register_confidence(fields):
    if not fields:
        return TIER_UNKNOWN
    worst = max(fields, key=lambda field: _TIER_ORDER.get(field.get("confidence"), 3))
    return worst.get("confidence", TIER_UNKNOWN)


def _register_signature(fields, constant_bits):
    """A behavioural fingerprint: two addresses with the same one are aliases."""
    parts = [
        "{}:{}:{}:{}".format(
            field["bit"], field.get("storage"), field.get("template"), field.get("read_bit")
        )
        for field in sorted(fields, key=lambda item: (item["bit"], str(item.get("storage"))))
    ]
    parts += [
        "c{}={}".format(index, value) for index, value in sorted(constant_bits.items())
    ]
    return "|".join(parts)


def _detect_aliases(registers):
    by_signature = {}
    for register in registers:
        by_signature.setdefault(register["signature"], []).append(register)

    classes = []
    for signature, group in sorted(by_signature.items(), key=lambda item: item[1][0]["address"]):
        addresses = sorted(register["address"] for register in group)
        canonical = addresses[0]
        for register in group:
            register["aliases"] = [
                address for address in addresses if address != register["address"]
            ]
            register["canonical_address"] = canonical
            register["is_alias"] = register["address"] != canonical
        if len(addresses) > 1:
            classes.append(
                {
                    "canonical_address": canonical,
                    "addresses": addresses,
                    "signature": signature,
                    "evidence": "identical write associations, next-state templates and "
                    "read-mux associations at every listed address",
                }
            )
    return classes


def _find_undecoded_address_bits(prober, registers):
    """Address bits the enumeration never varied, or that provably do nothing."""
    mapping = prober.mapping
    varied = set()
    addresses = mapping.addresses()
    for index in range(mapping.address_width):
        values = {(address >> index) & 1 for address in addresses}
        if len(values) > 1:
            varied.add(index)

    never_varied = sorted(set(range(mapping.address_width)) - varied)
    ignored = []
    if registers:
        probe_address = registers[0]["address"]
        ones = {index: 1 for index in range(mapping.data_width)}

        def signature(address):
            # Write all-ones into an all-zero machine and read an all-ones one:
            # an all-zero write into an all-zero machine changes nothing at any
            # address and would make every address bit look undecoded.
            write = prober.next_state(
                prober.evaluate(CONTEXT_LOW, address=address, pwrite=1, pwdata=ones)
            )
            read_low = prober.read_data(
                prober.evaluate(CONTEXT_LOW, address=address, pwrite=0)
            )
            read_high = prober.read_data(
                prober.evaluate(CONTEXT_HIGH, address=address, pwrite=0)
            )
            return (tuple(sorted(write.items())), tuple(read_low), tuple(read_high))

        baseline = signature(probe_address)
        for index in range(mapping.address_width):
            if signature(probe_address ^ (1 << index)) == baseline:
                ignored.append(index)
    return {
        "never_varied": never_varied,
        "never_varied_note": "the address stride left these PADDR bits constant across "
        "the whole enumeration, so whether they are decoded was not analysed",
        "no_effect_at_probe_address": ignored,
        "no_effect_note": "flipping these PADDR bits at the probe address changed "
        "neither the next state nor PRDATA: the bit is either undecoded or the two "
        "addresses alias",
        "probe_address": registers[0]["address"] if registers else None,
    }


def _coverage(prober, circuit, mapping, registers, dead_address_bits, clock_report, reset_notes):
    unsupported = {}
    for gate in circuit.unsupported_gates:
        entry = unsupported.setdefault(
            gate.type_name,
            {"gate_type": gate.type_name, "reason": gate.unsupported_reason,
             "count": 0, "examples": []},
        )
        entry["count"] += 1
        if len(entry["examples"]) < 3:
            entry["examples"].append(
                {"gate": gate.key, "uid": gate.uid, "name": gate.name}
            )

    associated = set()
    for register in registers:
        for field in register["fields"]:
            if field.get("storage"):
                associated.add(field["storage"])
    unassociated = [
        element.key for element in circuit.state_elements if element.key not in associated
    ]

    limits = [
        "only the enumerated address window was probed; addresses outside it, and "
        "byte offsets skipped by the address stride, were not analysed",
        "writes were probed with a single data bit and a single storage bit pinned at "
        "a time; a field whose next state depends on two data bits at once is not "
        "expressible in the next-state templates and is reported unresolved",
        "no multi-cycle behaviour was analysed: every claim is about the combinational "
        "next-state and read-mux functions of one APB access",
    ]
    if mapping.strobe_lanes == 0:
        limits.append(
            "the mapping declares no PSTRB, so byte-strobe behaviour was not analysed"
        )
    if circuit.combinational_loop:
        limits.append(
            "{} gate(s) sit on or behind a combinational loop and were not evaluated; "
            "every net behind them reads as unconstrained".format(
                len(circuit.combinational_loop)
            )
        )

    return {
        "addresses_probed": len(mapping.addresses()),
        "registers_found": len(registers),
        "flip_flops_total": len(circuit.state_elements),
        "flip_flops_associated": len(associated),
        "flip_flops_unassociated": unassociated,
        "unsupported_gate_types": sorted(unsupported.values(), key=lambda item: item["gate_type"]),
        "combinational_loop_gates": circuit.combinational_loop,
        "address_bits": dead_address_bits,
        "clocking": clock_report,
        "reset_notes": reset_notes,
        "limits": limits,
    }
