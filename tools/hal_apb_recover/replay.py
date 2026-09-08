"""Generate and run replayable tests for a recovered register map.

A recovered map is a claim about the netlist, so it should be executable
against it.  :func:`build_replay` turns the map into a list of transactions --
force a state, write, read, compare -- whose expected values come from the
*recovered model*, not from the netlist.  :func:`run_replay` then executes them
on the netlist.  A mismatch means the recovery is wrong; that is the point.

Every transaction carries the confidence of the claim it exercises, so a
failure can be read correctly: a mismatch on a ``proven_under_assumptions``
transaction is a defect in the analysis, while one on a ``heuristic``
transaction is the analysis admitting it guessed.

State is forced directly onto the flip-flops where a value is not reachable
through the bus (a status bit set by hardware, for instance).  Those steps are
labelled ``force_state`` precisely so nobody mistakes them for something
software could do.
"""

from .circuit import value_to_str
from .recover import TEMPLATES

__all__ = [
    "REPLAY_SCHEMA",
    "REPLAY_SCHEMA_VERSION",
    "ReplayError",
    "build_replay",
    "run_replay",
    "format_results",
]

REPLAY_SCHEMA = "fpgapa.apb-replay"
REPLAY_SCHEMA_VERSION = "1.0.0"


class ReplayError(RuntimeError):
    """The replay document cannot be executed against this netlist."""


# ---------------------------------------------------------------------------
# the recovered model, evaluated
# ---------------------------------------------------------------------------


def _storage_fields(register):
    return [field for field in register["fields"] if field.get("kind") == "storage"]


def _lane_selected(field, strobe, lanes_total):
    if lanes_total == 0:
        return True
    lanes = field.get("strobe_lanes")
    if not lanes:
        return False
    return any((strobe >> lane) & 1 for lane in lanes)


def _guard_satisfied(field, state):
    guard = field.get("guarded_by")
    if not guard:
        return True
    for candidate in guard.get("candidates", []):
        if state.get(candidate["storage"]) != candidate["enabling_value"]:
            return False
    return True


def model_write(document, state, address, data, strobe):
    """Next state predicted by the recovered map for one write."""
    lanes_total = document["mapping"]["strobe_lanes"]
    next_state = dict(state)
    for register in document["registers"]:
        if register["address"] != address:
            continue
        for field in _storage_fields(register):
            storage = field["storage"]
            if storage not in state:
                continue
            if not _lane_selected(field, strobe, lanes_total):
                continue
            if not _guard_satisfied(field, state):
                continue
            template = TEMPLATES.get(field.get("template"))
            if template is None:
                continue
            bit = (data >> field["bit"]) & 1
            next_state[storage] = template(bit, state[storage])
    return next_state


def model_read(document, state, address):
    """``(value, mask)`` predicted by the recovered map for one read."""
    value = 0
    mask = 0
    for register in document["registers"]:
        if register["address"] != address:
            continue
        for field in register["fields"]:
            index = field.get("read_bit")
            if index is None:
                continue
            if field.get("kind") == "constant":
                bit = field["constant_value"]
            else:
                storage = field.get("storage")
                if storage is None or storage not in state:
                    continue
                bit = state[storage]
                if field.get("polarity") == "inverted":
                    bit = 1 - bit
            mask |= 1 << index
            if bit:
                value |= 1 << index
    for entry in document["unmapped_addresses"]:
        if entry["address"] == address:
            mask = (1 << document["mapping"]["data_width"]) - 1
            value = 0
    return value, mask


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------


def _hexint(value, width):
    return "0x{:0{}x}".format(value, max(1, (width + 3) // 4))


def _reset_state(document):
    state = {}
    for register in document["registers"]:
        for field in _storage_fields(register):
            reset = field.get("reset_value")
            if reset is not None:
                state[field["storage"]] = reset
    return state


def _all_storage(document):
    keys = set()
    for register in document["registers"]:
        for field in _storage_fields(register):
            keys.add(field["storage"])
    return keys


class _Sequence(object):
    def __init__(self, document):
        self.document = document
        self.width = document["mapping"]["data_width"]
        self.lanes = document["mapping"]["strobe_lanes"]
        self.all_lanes = (1 << self.lanes) - 1 if self.lanes else 0
        self.transactions = []
        self.state = {}

    def force(self, bits, why, confidence="proven_under_assumptions"):
        self.state = dict(self.state)
        self.state.update(bits)
        self.transactions.append(
            {
                "id": "t{:04d}".format(len(self.transactions)),
                "op": "force_state",
                "bits": {key: value for key, value in sorted(bits.items())},
                "why": why,
                "confidence": confidence,
                "note": "not reachable through the bus; this is a testbench force",
            }
        )

    def write(self, address, data, strobe, why, confidence):
        self.state = model_write(self.document, self.state, address, data, strobe)
        self.transactions.append(
            {
                "id": "t{:04d}".format(len(self.transactions)),
                "op": "write",
                "address": address,
                "address_hex": _hexint(address, self.document["mapping"]["address_width"]),
                "data": _hexint(data, self.width),
                "strobe": _hexint(strobe, max(self.lanes, 1)),
                "why": why,
                "confidence": confidence,
                "expect_state": {key: value for key, value in sorted(self.state.items())},
            }
        )

    def read(self, address, why, confidence):
        value, mask = model_read(self.document, self.state, address)
        self.transactions.append(
            {
                "id": "t{:04d}".format(len(self.transactions)),
                "op": "read",
                "address": address,
                "address_hex": _hexint(address, self.document["mapping"]["address_width"]),
                "expect_data": _hexint(value, self.width),
                "expect_mask": _hexint(mask, self.width),
                "why": why,
                "confidence": confidence,
            }
        )


def build_replay(document):
    """Turn a register map into a replayable transaction list."""
    sequence = _Sequence(document)
    width = sequence.width
    ones = (1 << width) - 1
    strobe = sequence.all_lanes or 1
    reset_state = _reset_state(document)
    zero_state = {key: 0 for key in _all_storage(document)}
    one_state = {key: 1 for key in _all_storage(document)}

    canonical = [
        register
        for register in sorted(document["registers"], key=lambda item: item["address"])
        if not register.get("is_alias")
    ]

    sequence.force(reset_state, "start from the recovered reset values")
    for register in canonical:
        sequence.read(
            register["address"],
            "the reset value of {} must be readable".format(register["address_hex"]),
            register["confidence"],
        )

    for register in canonical:
        writable = [
            field
            for field in _storage_fields(register)
            if field.get("template") in TEMPLATES and field.get("template") != "hold"
        ]
        if not writable:
            continue
        confidence = _weakest(field.get("confidence") for field in writable)

        guard_bits = {}
        for field in writable:
            guard = field.get("guarded_by")
            for candidate in (guard or {}).get("candidates", []):
                guard_bits[candidate["storage"]] = candidate["enabling_value"]

        base = dict(zero_state)
        base.update(guard_bits)
        sequence.force(
            base,
            "clear every storage bit before writing {}{}".format(
                register["address_hex"],
                " (and satisfy the recovered guard)" if guard_bits else "",
            ),
        )
        sequence.write(
            register["address"],
            ones,
            strobe,
            "write all ones into {}".format(register["address_hex"]),
            confidence,
        )
        sequence.read(register["address"], "read back what the write stored", confidence)

        base = dict(one_state)
        base.update(guard_bits)
        sequence.force(base, "set every storage bit before writing zeros")
        sequence.write(
            register["address"],
            0,
            strobe,
            "write all zeros into {}: this is where a write-one-to-clear register "
            "differs from a plain one".format(register["address_hex"]),
            confidence,
        )
        sequence.read(register["address"], "read back after the zero write", confidence)

        # one transaction per byte lane, to show the strobes really gate the write
        for lane in range(sequence.lanes):
            base = dict(zero_state)
            base.update(guard_bits)
            sequence.force(base, "clear every storage bit before the lane-{} write".format(lane))
            sequence.write(
                register["address"],
                ones,
                1 << lane,
                "write all ones with only byte lane {} asserted".format(lane),
                confidence,
            )
            sequence.read(
                register["address"],
                "only the bits of lane {} may have changed".format(lane),
                confidence,
            )

        # a guarded register must not change while the guard is not satisfied
        if guard_bits:
            base = dict(zero_state)
            base.update({key: 1 - value for key, value in guard_bits.items()})
            sequence.force(base, "clear the recovered guard bit(s)")
            sequence.write(
                register["address"],
                ones,
                strobe,
                "write all ones with the guard not satisfied",
                confidence,
            )
            sequence.read(
                register["address"],
                "the guarded register must not have changed",
                confidence,
            )

    for entry in document.get("alias_classes", []):
        confidence = "proven_bounded"
        sequence.force(dict(zero_state), "clear every storage bit before the alias check")
        pattern = 0xA5A5 & ones
        sequence.write(
            entry["canonical_address"],
            pattern,
            strobe,
            "write a pattern at the canonical address of the alias class",
            confidence,
        )
        for address in entry["addresses"]:
            sequence.read(
                address,
                "every alias of 0x{:02x} must read the same value".format(
                    entry["canonical_address"]
                ),
                confidence,
            )

    for entry in document["unmapped_addresses"]:
        sequence.force(dict(one_state), "set every storage bit before the unmapped write")
        sequence.write(
            entry["address"],
            0,
            strobe,
            "write zeros at an address with no recovered register",
            "heuristic",
        )
        sequence.read(
            entry["address"],
            "an address with no recovered register is expected to read 0",
            "heuristic",
        )
        for register in canonical[:1]:
            sequence.read(
                register["address"],
                "the write at an unmapped address must not have disturbed {}".format(
                    register["address_hex"]
                ),
                "heuristic",
            )

    return {
        "schema": REPLAY_SCHEMA,
        "schema_version": REPLAY_SCHEMA_VERSION,
        "design": document.get("design"),
        "netlist": document["netlist"],
        "mapping": document["mapping"],
        "description": (
            "Replayable transactions generated from the recovered register map. Every "
            "expectation comes from the recovered model, so a mismatch means the "
            "recovery is wrong about the netlist."
        ),
        "transactions": sequence.transactions,
    }


def _weakest(confidences):
    order = ["proven_under_assumptions", "proven_bounded", "heuristic", "unknown"]
    present = [value for value in confidences if value in order]
    if not present:
        return "unknown"
    return max(present, key=order.index)


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


def _bus_values(circuit, mapping, address, pwrite, data=0, strobe=0):
    values = {}
    quiescent = mapping.quiescent_value
    bus = mapping.bus_nets()
    if quiescent is not None:
        for net in circuit.input_nets:
            if net not in bus:
                values[net] = quiescent
    if mapping.reset_net is not None:
        values[mapping.reset_net] = 1 if mapping.reset_active == "low" else 0
    values[mapping.scalars["psel"]] = 1
    values[mapping.scalars["penable"]] = 1
    values[mapping.scalars["pwrite"]] = pwrite
    for index, net in enumerate(mapping.vectors["paddr"]):
        values[net] = (address >> index) & 1
    for index, net in enumerate(mapping.vectors["pwdata"]):
        values[net] = (data >> index) & 1
    for index, net in enumerate(mapping.vectors["pstrb"]):
        values[net] = (strobe >> index) & 1
    return values


def run_replay(circuit, mapping, replay_document):
    """Execute a replay document against ``circuit``; return per-step results."""
    if replay_document.get("schema") != REPLAY_SCHEMA:
        raise ReplayError(
            "replay document schema is {!r}, expected {!r}".format(
                replay_document.get("schema"), REPLAY_SCHEMA
            )
        )
    known = {element.key for element in circuit.state_elements}
    state = {key: 0 for key in known}
    results = []

    for transaction in replay_document["transactions"]:
        operation = transaction["op"]
        record = {
            "id": transaction["id"],
            "op": operation,
            "why": transaction.get("why"),
            "confidence": transaction.get("confidence"),
            "ok": True,
        }
        if operation == "force_state":
            missing = [key for key in transaction["bits"] if key not in known]
            if missing:
                raise ReplayError(
                    "replay references storage bits this netlist does not have: {}. "
                    "A replay is tied to the netlist it was generated from.".format(
                        ", ".join(sorted(missing)[:5])
                    )
                )
            state.update(transaction["bits"])
        elif operation == "write":
            values = circuit.evaluate(
                dict(
                    _bus_values(
                        circuit,
                        mapping,
                        transaction["address"],
                        1,
                        int(transaction["data"], 16),
                        int(transaction["strobe"], 16),
                    ),
                    **circuit.state_vector_nets(state)
                )
            )
            next_state = circuit.next_state(values)
            undefined = sorted(key for key, value in next_state.items() if value is None)
            if undefined:
                record["ok"] = False
                record["detail"] = (
                    "the netlist left {} storage bit(s) undefined after this write "
                    "({}{})".format(
                        len(undefined),
                        ", ".join(undefined[:5]),
                        ", ..." if len(undefined) > 5 else "",
                    )
                )
            expected = transaction.get("expect_state") or {}
            mismatched = [
                key
                for key, value in sorted(expected.items())
                if key in next_state and next_state[key] != value
            ]
            if mismatched:
                record["ok"] = False
                record["detail"] = "storage bits differ from the recovered model: " + ", ".join(
                    "{} expected {} got {}".format(
                        key, expected[key], value_to_str(next_state[key])
                    )
                    for key in mismatched[:6]
                )
            state = {key: (0 if value is None else value) for key, value in next_state.items()}
        elif operation == "read":
            values = circuit.evaluate(
                dict(
                    _bus_values(circuit, mapping, transaction["address"], 0),
                    **circuit.state_vector_nets(state)
                )
            )
            mask = int(transaction["expect_mask"], 16)
            expected = int(transaction["expect_data"], 16)
            actual = 0
            undefined = []
            for index, net in enumerate(mapping.vectors["prdata"]):
                value = values.get(net)
                if value is None:
                    if (mask >> index) & 1:
                        undefined.append(index)
                    continue
                if value:
                    actual |= 1 << index
            record["actual"] = "0x{:0{}x}".format(
                actual, max(1, (mapping.data_width + 3) // 4)
            )
            if undefined:
                record["ok"] = False
                record["detail"] = "PRDATA bit(s) {} are undefined".format(
                    ", ".join(str(bit) for bit in undefined)
                )
            elif (actual & mask) != (expected & mask):
                record["ok"] = False
                record["detail"] = "read {}, expected {} under mask {}".format(
                    record["actual"], transaction["expect_data"], transaction["expect_mask"]
                )
        else:
            raise ReplayError("unknown replay operation {!r}".format(operation))
        results.append(record)
    return results


def format_results(results, allow_heuristic=False):
    """Return ``(ok, lines)`` -- a human summary of a replay run."""
    failures = [record for record in results if not record["ok"]]
    hard = [
        record
        for record in failures
        if not (allow_heuristic and record.get("confidence") in ("heuristic", "unknown"))
    ]
    lines = [
        "{} transactions, {} failed ({} of them on proven or bounded claims)".format(
            len(results), len(failures), len(hard)
        )
    ]
    for record in failures:
        lines.append(
            "  {} {} [{}] {}: {}".format(
                record["id"],
                record["op"],
                record.get("confidence"),
                record.get("why"),
                record.get("detail", "mismatch"),
            )
        )
    return not hard, lines
