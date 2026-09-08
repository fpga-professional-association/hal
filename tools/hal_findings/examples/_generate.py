"""Regenerate the example findings documents in this directory.

The examples are built through :mod:`hal_findings.model` rather than typed out
by hand, so they cannot drift away from the builders' invariants, and
``test_hal_findings.py`` asserts that regenerating them reproduces the checked
in files byte for byte -- which is also the determinism test for
:func:`hal_findings.serialize.dumps`.

    python tools/hal_findings/examples/_generate.py

The designs referenced below are the ones shipped in ``examples/*.zip`` at the
repository root, but the numbers are illustrative: these documents are
handwritten examples of the *schema*, not the output of a real analysis run.
Every document says so in its ``notes``.
"""

import hashlib
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )

import hal_findings  # noqa: E402
from hal_findings import model, serialize  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

ILLUSTRATIVE = (
    "illustrative example of the findings schema; the values are handwritten, not "
    "produced by a real analysis run"
)

GENERATED_AT = "2026-01-15T09:30:00Z"


def _fake_hash(seed):
    """A deterministic, obviously-derived stand-in for a real content hash."""
    return hashlib.sha256(("hal_findings example artifact: " + seed).encode("utf-8")).hexdigest()


def _artifact(artifact_id, path, design, gates, nets, library="XILINX_UNISIM"):
    return model.artifact(
        artifact_id,
        kind="netlist",
        path=path,
        sha256=_fake_hash(path),
        design_name=design,
        gate_count=gates,
        net_count=nets,
        gate_library={"name": library, "path": "share/hal/gate_libraries/" + library + ".hgl"},
    )


def _producer():
    return {"name": "hal_findings.examples", "version": hal_findings.__version__}


# ---------------------------------------------------------------------------
# 1. unbounded proof under assumptions
# ---------------------------------------------------------------------------


def equivalence_proof():
    id_a, id_b = "toy_cipher_original", "toy_cipher_resynthesized"
    assumptions = [
        model.assumption(
            "sequential-gate-name-correspondence",
            "Sequential gates of both netlists are matched by name; only combinational "
            "logic was allowed to change.",
            kind="naming",
            discharged=True,
        ),
        model.assumption(
            "combinational-frontier",
            "Only gates with the 'combinational' property are traversed; the outputs of "
            "all other gates are free variables.",
            kind="structural",
        ),
        model.assumption(
            "matched-state-correspondence",
            "Matched flip-flops are assumed to hold identical state, making this a "
            "next-state and output function equivalence.",
            kind="initial_state",
        ),
    ]
    finding = model.finding(
        "z3_utils/compare_netlists/equivalence",
        "Resynthesized netlist is functionally equivalent to the original",
        model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
        model.method(
            "SAT equivalence check (z3_utils.compare_netlists)",
            "formal",
            False,
            description=(
                "For every pair of same-named sequential gates the subgraph functions of "
                "their input nets are compared with Z3, together with the top-module "
                "output pins present in both netlists."
            ),
            parameters={"fail_on_unknown": True, "solver_timeout_s": 30},
        ),
        model.scope(
            [id_a, id_b],
            description="all matched sequential gate input cones and shared output pins",
        ),
        summary=(
            "All 412 equivalence queries were discharged as unsatisfiable with "
            "fail_on_unknown=True, so no undecided query was folded into the verdict."
        ),
        severity="info",
        assumptions=assumptions,
        bounds_dict=model.unbounded(
            description=(
                "Sequential gate outputs enter the queries as free variables, so the claim "
                "covers any number of clock cycles under the matched-state assumption."
            )
        ),
        solver_dict=model.solver(
            "z3", version="4.12.2", queries=412, unsat=412, sat=0, unknown=0, wall_time_s=48.7
        ),
        limits_dict=model.limits(timeout_s=30.0, hit=False),
        evidence_list=[
            model.evidence(
                "log",
                description="HAL log of the comparison run",
                path="evidence/compare_netlists.log",
                sha256=_fake_hash("compare_netlists.log"),
            ),
            model.evidence(
                "command",
                description="exact invocation",
                command=[
                    "hal",
                    "--python-script",
                    "scripts/compare.py",
                    "--",
                    "--fail-on-unknown",
                ],
            ),
        ],
        metrics={"matched_sequential_gates": 128, "compared_net_pairs": 412},
        tags=["equivalence", "resynthesis"],
    )
    return model.document(
        _producer(),
        [
            _artifact(id_a, "designs/toy_cipher.v", "toy_cipher", 1842, 1903),
            _artifact(id_b, "designs/toy_cipher_resyn.v", "toy_cipher", 1610, 1671),
        ],
        {
            "plugin": {"name": "z3_utils", "version": "0.9.0"},
            "entry_point": "z3_utils.compare_netlists",
            "configuration": {"fail_on_unknown": True, "solver_timeout_s": 30},
            "duration_s": 48.7,
        },
        [finding],
        generated_at=GENERATED_AT,
        notes=[
            ILLUSTRATIVE,
            "a boolean from compare_netlists is only a proof when fail_on_unknown=True",
        ],
    )


# ---------------------------------------------------------------------------
# 2. bounded proof and bounded counterexample side by side
# ---------------------------------------------------------------------------


def bounded_results():
    artifact_id = "fsm_locked"
    method = model.method(
        "sequential symbolic execution (unrolled)",
        "bounded_formal",
        True,
        description=(
            "The sequential part of the netlist is symbolically executed for a fixed "
            "number of clock cycles; nothing is claimed about deeper executions."
        ),
        parameters={"cycles": 8},
    )
    assumptions = [
        model.assumption(
            "reset-applied-in-cycle-0",
            "The netlist is driven from the reset state in cycle 0.",
            kind="initial_state",
        ),
        model.assumption(
            "free-primary-inputs",
            "All primary inputs other than clock and reset are unconstrained.",
            kind="environment",
        ),
    ]
    proof = model.finding(
        "sse/unlock-unreachable-within-8-cycles",
        "Unlock state is unreachable within 8 cycles",
        model.STATUS_PROVEN_BOUNDED,
        method,
        model.scope(
            [artifact_id],
            description="FSM state register",
            gates=[
                model.gate_ref(artifact_id, 41, "state_reg_0", gate_type="FDRE"),
                model.gate_ref(artifact_id, 42, "state_reg_1", gate_type="FDRE"),
                model.gate_ref(artifact_id, 43, "state_reg_2", gate_type="FDRE"),
            ],
        ),
        summary=(
            "No input sequence of length 8 drives the state register into the unlock "
            "encoding. This says nothing about cycle 9 or later."
        ),
        severity="info",
        assumptions=assumptions,
        bounds_dict=model.bounded(
            8,
            unroll_depth=8,
            description="claim holds only for executions of at most 8 clock cycles",
        ),
        solver_dict=model.solver("z3", version="4.12.2", queries=8, unsat=8, wall_time_s=12.4),
        limits_dict=model.limits(timeout_s=120.0, hit=False, cycle_limit=8),
        metrics={"unrolled_cycles": 8},
        tags=["bounded", "fsm"],
    )
    refutation = model.finding(
        "sse/output-mismatch-at-cycle-5",
        "Output mismatch against the reference model at cycle 5",
        model.STATUS_BOUNDED_COUNTEREXAMPLE,
        method,
        model.scope(
            [artifact_id],
            description="top-level data output",
            nets=[model.net_ref(artifact_id, 907, "data_out", role="output")],
        ),
        summary=(
            "Within the 8 cycle unrolling, an input sequence exists that makes data_out "
            "differ from the reference model in cycle 5."
        ),
        severity="high",
        assumptions=assumptions,
        bounds_dict=model.bounded(8, unroll_depth=8),
        counterexample_dict=model.counterexample(
            "Input sequence driving data_out to 0 where the reference model produces 1.",
            cycle_bound=5,
            witness=[
                model.witness_entry("key_in", "0x0f", cycle=0),
                model.witness_entry("start", "1", cycle=1),
                model.witness_entry(
                    "data_out",
                    "0",
                    cycle=5,
                    net=model.net_ref(artifact_id, 907, "data_out"),
                ),
            ],
            witness_available=True,
            evidence_list=[
                model.evidence(
                    "trace",
                    description="full witness trace",
                    path="evidence/cycle5_witness.json",
                    sha256=_fake_hash("cycle5_witness.json"),
                    media_type="application/json",
                )
            ],
        ),
        solver_dict=model.solver("z3", version="4.12.2", queries=1, sat=1, wall_time_s=3.1),
        limits_dict=model.limits(timeout_s=120.0, hit=False, cycle_limit=8),
        tags=["bounded", "fsm"],
    )
    return model.document(
        _producer(),
        [_artifact(artifact_id, "designs/fsm.v", "fsm", 640, 702)],
        {
            "plugin": {"name": "sequential_symbolic_execution", "version": "0.4.0"},
            "entry_point": "sequential_symbolic_execution.get_subgraph_z3_function",
            "configuration": {"cycles": 8},
            "duration_s": 15.5,
        },
        [proof, refutation],
        generated_at=GENERATED_AT,
        notes=[
            ILLUSTRATIVE,
            "both findings are bounded: neither may be rendered as an unbounded proof or "
            "an unbounded refutation",
        ],
    )


# ---------------------------------------------------------------------------
# 3. unbounded counterexample
# ---------------------------------------------------------------------------


def counterexample():
    id_a, id_b = "uart_golden", "uart_modified"
    finding = model.finding(
        "z3_utils/compare_netlists/equivalence",
        "Modified netlist is not equivalent to the golden netlist",
        model.STATUS_COUNTEREXAMPLE,
        model.method(
            "SAT equivalence check (z3_utils.compare_netlists)",
            "formal",
            False,
            parameters={"fail_on_unknown": False, "solver_timeout_s": 30},
        ),
        model.scope(
            [id_a, id_b],
            description="input cone of the transmit shift register",
            gates=[model.gate_ref(id_a, 233, "tx_shift_reg_3", gate_type="FDRE")],
            nets=[model.net_ref(id_a, 1104, "tx_shift_reg_3_D", role="data_input")],
        ),
        summary=(
            "With fail_on_unknown=False an undecided query would have been reported as "
            "equivalence, so the negative verdict is decided."
        ),
        severity="high",
        assumptions=[
            model.assumption(
                "sequential-gate-name-correspondence",
                "Sequential gates are matched by name.",
                kind="naming",
                discharged=True,
            )
        ],
        bounds_dict=model.unbounded(
            description="the compared functions differ regardless of cycle count"
        ),
        counterexample_dict=model.counterexample(
            "The subgraph functions of tx_shift_reg_3's data input differ.",
            witness=[
                model.witness_entry("tx_data[3]", "1"),
                model.witness_entry("tx_enable", "0"),
            ],
            witness_available=True,
        ),
        solver_dict=model.solver("z3", version="4.12.2", queries=97, sat=1, unsat=96),
        limits_dict=model.limits(timeout_s=30.0, hit=False),
        tags=["equivalence"],
    )
    return model.document(
        _producer(),
        [
            _artifact(id_a, "designs/uart_golden.v", "uart", 980, 1041),
            _artifact(id_b, "designs/uart_modified.v", "uart", 984, 1045),
        ],
        {
            "plugin": {"name": "z3_utils", "version": "0.9.0"},
            "entry_point": "z3_utils.compare_netlists",
            "configuration": {"fail_on_unknown": False, "solver_timeout_s": 30},
        },
        [finding],
        generated_at=GENERATED_AT,
        notes=[ILLUSTRATIVE],
    )


# ---------------------------------------------------------------------------
# 4. heuristic results
# ---------------------------------------------------------------------------


def heuristic_groups():
    artifact_id = "toy_cipher"
    method = model.method(
        "dataflow analysis (DANA)",
        "heuristic",
        False,
        description=(
            "Groups sequential gates into candidate word-level registers from shared "
            "control nets and common predecessors and successors."
        ),
        parameters={"min_group_size": 8, "expected_sizes": [8, 16, 32]},
    )
    group_a = model.finding(
        "dataflow/group/0001",
        "Candidate register group 1 (32 sequential gates)",
        model.STATUS_HEURISTIC,
        method,
        model.scope(
            [artifact_id],
            description="sequential gates grouped into one candidate register",
            gates=[
                model.gate_ref(artifact_id, 500 + index, "state_reg_{}".format(index),
                               gate_type="FDRE")
                for index in range(3)
            ],
            nets=[model.net_ref(artifact_id, 12, "clk", role="clock")],
            gate_types=["FDRE"],
        ),
        summary=(
            "32 flip-flops share clock and enable and have common predecessors; they look "
            "like one 32 bit register. Structural evidence only."
        ),
        severity="info",
        confidence=0.82,
        metrics={"group_size": 32},
        data={
            "group_id": 1,
            "successor_groups": [2],
            "predecessor_groups": [2],
            "group_id_note": (
                "dataflow group IDs are internal to this analysis run and unrelated to any "
                "HAL ID"
            ),
        },
        tags=["dataflow", "register-candidate"],
    )
    coverage = model.finding(
        "dataflow/coverage/unsupported-primitives",
        "Sequential gate types not covered by this dataflow run",
        model.STATUS_UNSUPPORTED,
        method,
        model.scope([artifact_id], gate_types=["RAMB18E1"]),
        summary=(
            "Block RAM primitives were not grouped; the absence of a register group for "
            "them is not evidence that no register exists."
        ),
        severity="info",
        unsupported_dict=model.unsupported(
            "primitive",
            "dataflow analysis only groups the sequential gate types it was configured for",
            [
                model.unsupported_primitive(
                    "RAMB18E1",
                    "block RAM primitive: not a flip-flop, so word-level register recovery "
                    "does not model it",
                    count=4,
                    properties=["ram", "sequential"],
                    example_gates=[
                        model.gate_ref(artifact_id, 1701, "mem_inst/bram_0",
                                       gate_type="RAMB18E1")
                    ],
                )
            ],
        ),
        tags=["coverage", "dataflow"],
    )
    return model.document(
        _producer(),
        [_artifact(artifact_id, "designs/toy_cipher.v", "toy_cipher", 1842, 1903)],
        {
            "plugin": {"name": "dataflow_analysis", "version": "1.3.0"},
            "entry_point": "dataflow.analyze",
            "configuration": {"min_group_size": 8, "expected_sizes": [8, 16, 32]},
            "duration_s": 6.2,
        },
        [group_a, coverage],
        generated_at=GENERATED_AT,
        notes=[
            ILLUSTRATIVE,
            "dataflow group IDs are local to this run and are not HAL object IDs",
        ],
    )


# ---------------------------------------------------------------------------
# 5. inconclusive outcomes: timeout, unknown, error
# ---------------------------------------------------------------------------


def inconclusive():
    id_a, id_b = "crypto_trojan_golden", "crypto_trojan_suspect"
    method = model.method(
        "SAT equivalence check (z3_utils.compare_nets)",
        "formal",
        False,
        parameters={"fail_on_unknown": True, "solver_timeout_s": 10},
    )
    assumptions = [
        model.assumption(
            "sequential-gate-name-correspondence",
            "Sequential gates are matched by name.",
            kind="naming",
        )
    ]
    timeout = model.finding(
        "z3_utils/compare_nets/key_schedule_out",
        "Equivalence of key_schedule_out could not be decided before the timeout",
        model.STATUS_TIMEOUT,
        method,
        model.scope(
            [id_a, id_b],
            nets=[model.net_ref(id_a, 3312, "key_schedule_out", role="output")],
        ),
        summary="The solver hit the 10 s per-query limit; no verdict was reached.",
        severity="info",
        assumptions=assumptions,
        solver_dict=model.solver("z3", version="4.12.2", queries=1, unknown=1, wall_time_s=10.0),
        limits_dict=model.limits(timeout_s=10.0, wall_time_s=10.0, hit=True),
        tags=["equivalence"],
    )
    unknown = model.finding(
        "z3_utils/compare_netlists/equivalence",
        "Equivalence of the two netlists is undecided",
        model.STATUS_UNKNOWN,
        method,
        model.scope([id_a, id_b]),
        summary=(
            "compare_netlists returned False with fail_on_unknown=True, which conflates a "
            "functional difference, a structural mismatch and an undecided query. No "
            "verdict is reported."
        ),
        severity="info",
        assumptions=assumptions,
        limits_dict=model.limits(timeout_s=10.0, hit=True),
        tags=["equivalence"],
    )
    error = model.finding(
        "z3_utils/compare_nets/trojan_payload_en",
        "Comparison of trojan_payload_en failed",
        model.STATUS_ERROR,
        method,
        model.scope(
            [id_a, id_b],
            nets=[model.net_ref(id_a, 4001, "trojan_payload_en")],
        ),
        summary="The analysis itself failed; this says nothing about the design.",
        severity="medium",
        error_dict=model.error(
            "plugin_error",
            "cannot get Boolean z3 function of net trojan_payload_en: net is multi driven",
            detail="raised by z3_utils::get_prefixed_function_of_net",
        ),
        evidence_list=[
            model.evidence(
                "log", path="evidence/z3_utils_error.log", description="HAL error log"
            )
        ],
        tags=["equivalence"],
    )
    return model.document(
        _producer(),
        [
            _artifact(id_a, "designs/crypto_golden.v", "crypto_trojan", 5210, 5480),
            _artifact(id_b, "designs/crypto_suspect.v", "crypto_trojan", 5233, 5502),
        ],
        {
            "plugin": {"name": "z3_utils", "version": "0.9.0"},
            "entry_point": "z3_utils.compare_nets",
            "configuration": {"fail_on_unknown": True, "solver_timeout_s": 10},
            "duration_s": 31.9,
        },
        [timeout, unknown, error],
        generated_at=GENERATED_AT,
        notes=[
            ILLUSTRATIVE,
            "none of these findings states anything about the design itself",
        ],
    )


# ---------------------------------------------------------------------------
# 6. unsupported primitives blocking an analysis
# ---------------------------------------------------------------------------


def unsupported_primitives():
    artifact_id = "simple_alu"
    method = model.method(
        "netlist precondition check",
        "structural",
        False,
        description="Direct inspection of the netlist; no solver involved.",
    )
    finding = model.finding(
        "z3_utils/compare_netlists/coverage/unmodelled-primitives",
        "Gate types that the equivalence check neither traverses nor matches",
        model.STATUS_UNSUPPORTED,
        method,
        model.scope([artifact_id], gate_types=["DSP48E1", "PLLE2_BASE"]),
        summary=(
            "Two gate types carry neither the 'combinational' nor the 'sequential' "
            "property; their outputs enter every query as free variables."
        ),
        severity="medium",
        unsupported_dict=model.unsupported(
            "primitive",
            "compare_netlists traverses combinational gates and matches sequential gates "
            "by name; any other gate type is modelled as a free variable",
            [
                model.unsupported_primitive(
                    "DSP48E1",
                    "gate type has neither the 'combinational' nor the 'sequential' "
                    "property, so its output is an unconstrained free variable",
                    count=2,
                    properties=["dsp"],
                    example_gates=[
                        model.gate_ref(artifact_id, 88, "alu/dsp_0", gate_type="DSP48E1")
                    ],
                ),
                model.unsupported_primitive(
                    "PLLE2_BASE",
                    "clock generation primitive outside the Boolean model",
                    count=1,
                    properties=["pll"],
                ),
            ],
        ),
        data={"analysis_blocked": True},
        tags=["coverage"],
    )
    return model.document(
        _producer(),
        [_artifact(artifact_id, "designs/simple_alu.v", "simple_alu", 320, 366)],
        {
            "plugin": {"name": "z3_utils", "version": "0.9.0"},
            "entry_point": "z3_utils.compare_netlists",
        },
        [finding],
        generated_at=GENERATED_AT,
        notes=[
            ILLUSTRATIVE,
            "unsupported coverage is reported explicitly instead of being silently "
            "excluded from the result",
        ],
    )


#: ``filename -> builder`` for every shipped example.
EXAMPLES = {
    "equivalence-proof.json": equivalence_proof,
    "bounded-results.json": bounded_results,
    "counterexample.json": counterexample,
    "dataflow-heuristic.json": heuristic_groups,
    "inconclusive-timeout-unknown-error.json": inconclusive,
    "unsupported-primitives.json": unsupported_primitives,
}


def build_all():
    """Return ``{filename: document}`` for every example."""
    return {name: builder() for name, builder in sorted(EXAMPLES.items())}


def main():
    for name, document in sorted(build_all().items()):
        serialize.write_document(document, os.path.join(HERE, name))
        print("wrote", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
