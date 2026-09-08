#!/usr/bin/env python3
"""Regenerate the recorded findings documents for ``accumulator.v``.

``tools/hal_explain`` composes *results*, so its fixtures are results: three
findings documents that stand in for what dataflow analysis, module
identification and ``hal_fsm`` report about ``accumulator.v``.  They are
recorded rather than produced live for two reasons -- the composition has to be
testable without a HAL build, and the expected content has to be reviewable in a
diff.

They are not invented, though.  Two of the three go through the *real* adapters
(``hal_findings.adapters.dataflow`` and
``hal_explain.adapters.module_identification``) driven by stub plugin results
built from the fixture netlist, so a change in either adapter shows up here as a
diff.  The hal_fsm document is assembled directly with ``hal_findings.model``
because reproducing ``solve_fsm``'s in-HAL path outside HAL is not worth the
machinery; its finding IDs, statuses, assumptions and ``data`` layout mirror
``tools/hal_fsm/findings.py`` field by field, and
``tests/headless_smoke/explain_blocks_smoke.py`` composes the *real* hal_fsm
output in the container so a drift between the two is caught there.

Run from the repository root::

    python tools/hal_explain/fixtures/_generate.py
    python tools/hal_explain/fixtures/_generate.py --check   # CI drift check
"""

import argparse
import contextlib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(os.path.dirname(HERE))
REPO_ROOT = os.path.dirname(TOOLS)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from hal_cdc.fixture_netlist import load_fixture  # noqa: E402
from hal_findings import model as findings_model  # noqa: E402
from hal_findings import serialize as findings_serialize  # noqa: E402
from hal_findings import validate as findings_validate  # noqa: E402
from hal_findings.adapters import dataflow as dataflow_adapter  # noqa: E402

from hal_explain.adapters import module_identification as modid_adapter  # noqa: E402

NETLIST = os.path.join(HERE, "accumulator.v")
GATE_LIBRARY = os.path.join(
    REPO_ROOT, "plugins", "gate_libraries", "definitions", "example_library.hgl"
)

#: The paths *recorded* in the documents are repository-relative and POSIX, so
#: the committed fixtures are byte-identical on every machine and ``--check``
#: means something in CI.  :func:`generate` runs inside a mirror of those inputs
#: (see :func:`_normalized_inputs`) so that the adapters can still hash the files
#: they name.
NETLIST_REL = "tools/hal_explain/fixtures/accumulator.v"
GATE_LIBRARY_REL = "plugins/gate_libraries/definitions/example_library.hgl"

#: The inputs the documents name and hash, as ``(recorded path, path on disk)``.
INPUTS = ((NETLIST_REL, NETLIST), (GATE_LIBRARY_REL, GATE_LIBRARY))

#: Frozen so that regenerating produces a byte-identical file.
GENERATED_AT = "2026-01-01T00:00:00Z"

ACCUMULATOR = ["acc_r0", "acc_r1", "acc_r2", "acc_r3"]
CONTROLLER = ["st_a", "st_b"]
ADDER = [
    "a_x0", "a_c0", "a_t1", "a_x1", "a_p1", "a_g1", "a_o1",
    "a_t2", "a_x2", "a_p2", "a_g2", "a_o2", "a_t3", "a_x3",
]
COMPARATOR = ["cmp_n0", "cmp_n2", "cmp_and"]
PARITY = ["p_x0", "p_x1", "p_x2"]


# ---------------------------------------------------------------------------
# duck-typed stand-ins for the plugin objects the adapters read
# ---------------------------------------------------------------------------


class _Pin(object):
    def __init__(self, pin):
        self._pin = pin

    def get_name(self):
        return self._pin.name

    def get_direction(self):
        return self._pin.direction

    def get_type(self):
        return self._pin.type


class _GateType(object):
    def __init__(self, view):
        self._view = view

    def get_name(self):
        return self._view.name

    def get_property_list(self):
        return sorted(self._view.properties)

    def get_pins(self):
        return [_Pin(pin) for pin in self._view.pins]


class _Gate(object):
    def __init__(self, view):
        self._view = view
        self._type = _GateType(view.type)

    def get_id(self):
        return self._view.id

    def get_name(self):
        return self._view.name

    def get_type(self):
        return self._type

    def get_module(self):
        return None


class _Net(object):
    def __init__(self, view):
        self._view = view

    def get_id(self):
        return self._view.id

    def get_name(self):
        return self._view.name


class _GateLibrary(object):
    def __init__(self, path):
        self._path = path

    def get_name(self):
        return "EXAMPLE_GATE_LIBRARY"

    def get_path(self):
        return self._path


class _Netlist(object):
    def __init__(self, view, path, library_path):
        self._view = view
        self._path = path
        self._library = _GateLibrary(library_path)
        self.gates = {gate.id: _Gate(gate) for gate in view.sorted_gates()}
        self.nets = {net.id: _Net(net) for net in view.sorted_nets()}
        self._by_name = {gate.name: self.gates[gate.id] for gate in view.sorted_gates()}
        self._nets_by_name = {net.name: self.nets[net.id] for net in view.sorted_nets()}

    def get_id(self):
        return 1

    def get_gates(self):
        return [self.gates[key] for key in sorted(self.gates)]

    def get_nets(self):
        return [self.nets[key] for key in sorted(self.nets)]

    def get_gate_library(self):
        return self._library

    def get_input_filename(self):
        return self._path

    def get_design_name(self):
        return "accumulator_top"

    def get_device_name(self):
        return ""

    def gate(self, name):
        return self._by_name[name]

    def net(self, name):
        return self._nets_by_name[name]


class _DataflowConfiguration(object):
    min_group_size = 2
    expected_sizes = []
    enable_stages = True
    enforce_type_consistency = True


class _DataflowResult(object):
    """What ``hal_findings.adapters.dataflow`` reads off a ``dataflow.Result``."""

    def __init__(self, netlist, groups, control_nets):
        self._netlist = netlist
        self._groups = groups
        self._control = control_nets

    def get_netlist(self):
        return self._netlist

    def get_groups(self):
        return self._groups

    def get_group_successors(self, group_id):
        return {2} if group_id == 1 else set()

    def get_group_predecessors(self, group_id):
        return {1} if group_id == 2 else set()

    def get_group_control_nets(self, group_id, pin_type):
        return self._control.get((group_id, pin_type), set())


class _Candidate(object):
    """What the module_identification adapter reads off a ``VerifiedCandidate``."""

    def __init__(self, name, info, types, gates, operands, output_nets, control_signals,
                 verified):
        self._name = name
        self._info = info
        self.types = list(types)
        self.gates = list(gates)
        self.base_gates = list(gates)
        self.operands = [list(operand) for operand in operands]
        self.output_nets = list(output_nets)
        self.control_signals = list(control_signals)
        self.total_input_nets = []
        self.total_output_nets = list(output_nets)
        self.verified = bool(verified)

    def is_verified(self):
        return self.verified

    def get_name(self):
        return self._name

    def get_candidate_info(self):
        return self._info


class _ModuleIdentificationResult(object):
    def __init__(self, netlist, candidates):
        self._netlist = netlist
        self._candidates = dict(candidates)

    def get_netlist(self):
        return self._netlist

    def get_candidates(self):
        return dict(self._candidates)

    def get_candidate_gates(self):
        return {key: list(value.gates) for key, value in self._candidates.items()}

    def get_verified_candidates(self):
        return {
            key: value for key, value in self._candidates.items() if value.is_verified()
        }

    def get_verified_candidate_gates(self):
        return {
            key: list(value.gates)
            for key, value in self._candidates.items()
            if value.is_verified()
        }

    def get_timing_stats(self):
        return '{"total_s": 0.42}'


# ---------------------------------------------------------------------------
# the three documents
# ---------------------------------------------------------------------------


def build_dataflow_document(netlist):
    """DANA groups the accumulator and the controller flip-flops."""
    groups = {
        1: {netlist.gate(name) for name in ACCUMULATOR},
        2: {netlist.gate(name) for name in CONTROLLER},
    }
    control = {(1, "enable"): {netlist.net("i_en")}}
    result = _DataflowResult(netlist, groups, control)
    return dataflow_adapter.build_document(
        result,
        artifact_id="netlist",
        configuration=_DataflowConfiguration(),
        plugin_version="0.1",
        control_pin_types=(("enable", "enable"),),
        netlist_path=NETLIST_REL,
        generated_at=GENERATED_AT,
    )


def build_module_identification_document(netlist):
    """Two verified operations and one cone that could not be verified.

    The adder candidate deliberately contains the accumulator flip-flops as
    well: ``module_identification`` builds candidates *around* known registers,
    so its gate set overlaps DANA's register group.  That overlap is the point --
    the composed model has to record it instead of silently picking a winner.
    """
    candidates = {
        1: _Candidate(
            "ADD(o_acc[3:0], d[3:0])",
            "verified addition, 4 bit, no carry out",
            ["addition"],
            [netlist.gate(name) for name in ADDER + ACCUMULATOR],
            [
                [netlist.net(name) for name in ("o_acc0", "o_acc1", "o_acc2", "o_acc3")],
                [netlist.net(name) for name in ("d0", "d1", "d2", "d3")],
            ],
            [netlist.net(name) for name in ("s0", "s1", "s2", "s3")],
            [netlist.net("i_en")],
            True,
        ),
        2: _Candidate(
            "EQ(o_acc[3:0], 4'b1010)",
            "verified equality comparison against a constant",
            ["equal"],
            [netlist.gate(name) for name in COMPARATOR],
            [[netlist.net(name) for name in ("o_acc0", "o_acc1", "o_acc2", "o_acc3")]],
            [netlist.net("o_match")],
            [],
            True,
        ),
        3: _Candidate(
            "",
            "checked, no reference operation matched",
            ["none"],
            [netlist.gate(name) for name in PARITY],
            [],
            [netlist.net("o_par")],
            [],
            False,
        ),
    }
    result = _ModuleIdentificationResult(netlist, candidates)
    return modid_adapter.build_document(
        result,
        artifact_id="netlist",
        configuration={"max_control_signals": 3, "types_to_check": ["addition", "equal"]},
        plugin_version="0.1",
        netlist_path=NETLIST_REL,
        generated_at=GENERATED_AT,
    )


def _gate_refs(netlist, names):
    return [
        findings_model.gate_ref(
            "netlist",
            netlist.gate(name).get_id(),
            name,
            gate_type=netlist.gate(name).get_type().get_name(),
        )
        for name in names
    ]


def build_fsm_document(netlist):
    """The controller as ``tools/hal_fsm`` reports it.

    The field layout mirrors ``tools/hal_fsm/findings.py``: a ``heuristic``
    candidate, a ``proven_under_assumptions`` transition relation naming the
    candidate as an undischarged assumption, a reachability claim, and the
    coverage finding for the sequential gates no machine covered.
    """
    from hal_explain import __version__

    artifact = findings_model.artifact(
        "netlist",
        kind="netlist",
        path=NETLIST_REL,
        sha256=findings_serialize.sha256_file(NETLIST_REL),
        design_name="accumulator_top",
        gate_count=len(netlist.get_gates()),
        net_count=len(netlist.get_nets()),
    )
    register = _gate_refs(netlist, CONTROLLER)

    candidate_method = findings_model.method(
        "state register proposal",
        "heuristic",
        False,
        description="strongly connected components of the flip-flop dependency graph, "
        "scored by a published weighted sum of structural features",
    )
    solver_method = findings_model.method(
        "solve_fsm (smt)",
        "symbolic",
        False,
        description="solve_fsm builds the next-state function of every state flip-flop "
        "from the transition cone and enumerates successors with an SMT solver.",
    )

    assumptions = [
        findings_model.assumption(
            "state-register",
            "the flip-flops st_a, st_b are the state register; this comes from "
            "fsm/candidate/001 and is a heuristic",
            kind="structural",
            discharged=False,
        ),
        findings_model.assumption(
            "state-bit-order",
            "state bit i is the output of bit_order[i]",
            kind="tool",
            discharged=True,
        ),
        findings_model.assumption(
            "asynchronous-control-inactive",
            "every set/reset pin of the state register is tied to a constant, so the "
            "reset solve_fsm does not model can never fire",
            kind="structural",
            discharged=True,
        ),
        findings_model.assumption(
            "library-next-state",
            "the gate library's next_state expression for FFR is taken on trust",
            kind="library",
            discharged=False,
        ),
    ]

    findings = [
        findings_model.finding(
            "fsm/candidate/001",
            "Candidate state register: 2 flip-flop(s) [st_a, st_b]",
            findings_model.STATUS_HEURISTIC,
            candidate_method,
            findings_model.scope(
                ["netlist"],
                description="flip-flops proposed as one state register",
                gates=register,
            ),
            summary="Two mutually dependent flip-flops with feedback through "
            "combinational logic. Structural evidence only.",
            confidence=0.82,
            severity="info",
            data={"sources": ["scc", "self_loop_cluster"]},
            metrics={"size": 2},
            tags=["fsm", "candidate", "register-candidate"],
        ),
        findings_model.finding(
            "fsm/machine01/transitions",
            "Recovered state transition relation: 3 states, 5 transitions",
            findings_model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            solver_method,
            findings_model.scope(
                ["netlist"],
                description="the state register and its transition relation",
                gates=register,
            ),
            summary="Every transition was derived from the netlist by solve_fsm's SMT "
            "exploration forward from the initial state.",
            severity="info",
            assumptions=assumptions,
            bounds_dict=findings_model.unbounded(
                description="the transition relation holds in every clock cycle"
            ),
            evidence_list=[
                findings_model.evidence(
                    "dot",
                    description="state diagram written by hal_fsm",
                    path="build/hal_fsm/accumulator/state-diagram-machine01.dot",
                )
            ],
            metrics={"states": 3, "transitions": 5, "state_bits": 2},
            data={
                "bit_order": CONTROLLER,
                "bit_order_note": "state bit i is the output of bit_order[i]",
                "initial_state": 0,
                "initial_state_bits": "00",
                "solver_mode": "smt",
                "states": [0, 1, 2],
                "transitions": [
                    {"source": 0, "target": 0, "condition": "!net_3"},
                    {"source": 0, "target": 1, "condition": "net_3"},
                    {"source": 1, "target": 1, "condition": "!net_4"},
                    {"source": 1, "target": 2, "condition": "net_4"},
                    {"source": 2, "target": 0, "condition": "1"},
                ],
                "signals": {
                    "net_3": {"name": "i_go", "net_id": 3, "role": "input"},
                    "net_4": {"name": "i_fin", "net_id": 4, "role": "input"},
                },
            },
            tags=["fsm", "transitions", "smt"],
        ),
        findings_model.finding(
            "fsm/machine01/reachable-states",
            "3 state(s) are reachable from the initial state",
            findings_model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            solver_method,
            findings_model.scope(
                ["netlist"],
                description="the reachable closure of the recovered relation",
                gates=register,
            ),
            summary="The reachable closure was recomputed from the recovered relation "
            "and matches the states solve_fsm explored.",
            severity="info",
            assumptions=assumptions,
            bounds_dict=findings_model.unbounded(),
            metrics={"reachable_states": 3},
            tags=["fsm", "reachability"],
        ),
        findings_model.finding(
            "fsm/coverage/uncovered-sequential-gates",
            "Sequential gates outside every solved state register",
            findings_model.STATUS_UNSUPPORTED,
            findings_model.method(
                "coverage check", "structural", False,
                description="sequential gates that ended up in no solved machine",
            ),
            findings_model.scope(
                ["netlist"],
                description="sequential gates no machine covered",
                gates=_gate_refs(netlist, ACCUMULATOR),
            ),
            summary="4 sequential gate(s) are not part of any solved state machine. "
            "That is a limit of this run, not evidence that they hold no state.",
            severity="info",
            unsupported_dict=findings_model.unsupported(
                "primitive",
                "these sequential gates were not part of any solved state register",
                [
                    findings_model.unsupported_primitive(
                        "FF",
                        "not part of a solved state register in this run",
                        count=4,
                        properties=["ff", "sequential"],
                        example_gates=_gate_refs(netlist, ACCUMULATOR[:3]),
                    )
                ],
            ),
            tags=["fsm", "coverage"],
        ),
    ]

    return findings_model.document(
        {"name": "hal_fsm", "version": __version__},
        [artifact],
        {
            "plugin": {
                "name": "solve_fsm",
                "version": "0.1",
                "description": "state transition graph reconstruction (solve_fsm)",
            },
            "entry_point": "solve_fsm.solve_fsm",
        },
        findings,
        generated_at=GENERATED_AT,
        notes=[
            "state values are relative to the bit order recorded on the transitions "
            "finding; gate references are scoped to artifact 'netlist'",
            "hal_fsm scopes its findings to the state register only: the transition "
            "logic it handed to solve_fsm is not named in this document, so a composed "
            "block model shows those gates as unclassified",
        ],
    )


DOCUMENTS = (
    ("findings-dataflow.json", build_dataflow_document),
    ("findings-module-identification.json", build_module_identification_document),
    ("findings-fsm.json", build_fsm_document),
)


def _copy_with_lf(source, destination):
    """Copy ``source`` to ``destination``, rewriting CRLF line endings as LF."""
    with open(source, "rb") as handle:
        data = handle.read()
    directory = os.path.dirname(destination)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(destination, "wb") as handle:
        handle.write(data.replace(b"\r\n", b"\n"))


@contextlib.contextmanager
def _normalized_inputs():
    """Yield a temporary mirror of the hashed inputs, with LF line endings.

    The documents pin their inputs by digest, and a digest of a working-tree
    file is only reproducible if the working tree is: git hands a Windows
    checkout CRLF line endings, so hashing ``example_library.hgl`` where it lies
    yields one digest on Windows and another on Linux, and the recorded fixtures
    can then only ever match one of the two.  The generator therefore hashes the
    canonical LF form of every input -- what the blob in git says, and what a
    Linux checkout has on disk -- by mirroring the inputs into a scratch tree at
    the same repository-relative paths and generating from there.
    """
    root = tempfile.mkdtemp(prefix="hal_explain_fixtures_")
    try:
        for relative, path in INPUTS:
            _copy_with_lf(path, os.path.join(root, *relative.split("/")))
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def generate():
    """Return ``{filename: serialized document text}``.

    The working directory is switched to the mirror of the inputs for the
    duration: the documents record repository-relative paths so they are
    byte-identical on every machine, and
    ``hal_findings.adapters.common.netlist_artifact`` hashes exactly the path it
    records.  Without the switch the artifact would silently come out with an
    ``unhashed_reason`` instead of a digest, depending on where the script
    happened to be run from.
    """
    previous = os.getcwd()
    with _normalized_inputs() as root:
        os.chdir(root)
        try:
            view = load_fixture(NETLIST_REL, GATE_LIBRARY_REL)
            netlist = _Netlist(view, NETLIST_REL, GATE_LIBRARY_REL)
            output = {}
            for filename, builder in DOCUMENTS:
                document = builder(netlist)
                findings_validate.validate_document(document)
                output[filename] = findings_serialize.dumps(document)
            return output
        finally:
            os.chdir(previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the recorded files differ from what this script produces",
    )
    args = parser.parse_args(argv)

    documents = generate()
    failures = 0
    for filename, text in sorted(documents.items()):
        path = os.path.join(HERE, filename)
        if args.check:
            existing = None
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as handle:
                    existing = handle.read()
            if existing != text:
                sys.stderr.write(
                    "{} is out of date; rerun tools/hal_explain/fixtures/_generate.py\n".format(
                        filename
                    )
                )
                failures += 1
            continue
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        sys.stdout.write("wrote {}\n".format(path))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
