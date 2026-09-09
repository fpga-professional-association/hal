#!/usr/bin/env python3
"""Re-run the reverse-engineering result of the 05_lfsr_prng walkthrough.

Everything this script claims is derived from ``netlist.hal.v`` alone: it never
reads ``design.v`` or ``spec.md``.  What it does read from the walkthrough are
the *answers* the guide publishes (16 stages, taps {15, 14, 12, 3}, seed
0xACE1, period 65535) -- so a rerun that no longer reproduces them fails, which
is what makes this usable as a CI smoke test.

Run it in the build container:

    HAL_BASE_PATH=/work/build PYTHONPATH=/work/build/lib \\
        python3 examples/agilex3_walkthroughs/05_lfsr_prng/check.py

Options let you point it at another build, another netlist or an output
directory for the artifacts the guide embeds:

    python3 check.py --hal-lib /work/build/lib --out-dir /tmp/out --write-artifacts

Exit codes: 0 every check passed, 1 a check failed, 2 the environment is not
usable (no ``hal_py``, netlist will not load).
"""

import argparse
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))

# --- what the walkthrough claims -------------------------------------------
EXPECTED_STAGES = 16
EXPECTED_TAPS = (15, 14, 12, 3)          # 0-based register indices
EXPECTED_POLYNOMIAL = "x^16 + x^15 + x^13 + x^4 + 1"
EXPECTED_SEED = 0xACE1
EXPECTED_PERIOD = 65535
EXPECTED_FF = 16
EXPECTED_LCELL = 17


class CheckFailed(Exception):
    pass


class Checks(object):
    """Tiny assertion recorder so a failure still prints the whole picture."""

    def __init__(self):
        self.results = []

    def __call__(self, name, condition, detail=""):
        self.results.append((bool(condition), name, detail))
        print("  [{}] {}{}".format("ok" if condition else "FAIL", name,
                                   (" -- " + detail) if detail else ""))
        return bool(condition)

    @property
    def failed(self):
        return [r for r in self.results if not r[0]]


# ---------------------------------------------------------------------------
# netlist plumbing
# ---------------------------------------------------------------------------

def load(args):
    sys.path.insert(0, os.path.join(args.repo, "tools"))
    from hal_agilex import hal_adapter

    lib_dirs = [d for d in args.hal_lib if d]
    hal_py = hal_adapter.import_hal(lib_dirs)
    netlist = hal_adapter.load_netlist(hal_py, args.netlist, args.gate_library)
    report = hal_adapter.elaborate(hal_py, netlist)
    return hal_py, netlist, report


def combinational_cells(hal_py, netlist):
    """Every non-sequential gate that has a Boolean function on an output pin.

    Sequential gates are skipped explicitly: their ``get_boolean_functions()``
    describes the flip-flop's internals (next state, clock, clear), which is not
    something to evaluate as if it were a LUT.
    """
    cells = []
    for gate in netlist.get_gates():
        if gate.get_type().has_property(hal_py.GateTypeProperty.sequential):
            continue
        functions = gate.get_boolean_functions()
        if not functions:
            continue
        outputs = {}
        for pin, function in functions.items():
            net = gate.get_fan_out_net(pin)
            if net is not None:
                outputs[pin] = (net, function)
        if not outputs:
            continue
        inputs = {}
        for pin in gate.get_type().get_input_pin_names():
            net = gate.get_fan_in_net(pin)
            if net is not None:
                inputs[pin] = net
        cells.append((gate, inputs, outputs))
    return cells


class Evaluator(object):
    """Evaluate the combinational layer for a given flip-flop state."""

    def __init__(self, hal_py, netlist, ff_gates):
        self.hal_py = hal_py
        self.netlist = netlist
        self.cells = combinational_cells(hal_py, netlist)
        self.ff_q = {}
        for index, gate in enumerate(ff_gates):
            self.ff_q[gate.get_fan_out_net("q").get_id()] = index
        self.const = {}
        for net in netlist.get_nets():
            if net.is_gnd_net():
                self.const[net.get_id()] = 0
            elif net.is_vcc_net():
                self.const[net.get_id()] = 1

    def run(self, state_bits):
        """state_bits: list of 0/1 per flip-flop -> dict net id -> 0/1."""
        values = dict(self.const)
        for net_id, index in self.ff_q.items():
            values[net_id] = state_bits[index]
        ZERO = self.hal_py.BooleanFunction.Value.ZERO
        ONE = self.hal_py.BooleanFunction.Value.ONE
        pending = list(self.cells)
        for _ in range(len(self.cells) + 2):
            if not pending:
                break
            still = []
            for gate, inputs, outputs in pending:
                assignment = {}
                ready = True
                for pin, net in inputs.items():
                    value = values.get(net.get_id())
                    if value is None:
                        ready = False
                        break
                    assignment[pin] = ONE if value else ZERO
                if not ready:
                    still.append((gate, inputs, outputs))
                    continue
                for pin, (net, function) in outputs.items():
                    result = function.evaluate(assignment)
                    if result is None:
                        raise CheckFailed(
                            "evaluating {} of gate {} failed".format(pin, gate.get_name()))
                    values[net.get_id()] = 1 if result == ONE else 0
            pending = still
        if pending:
            raise CheckFailed("combinational layer did not settle -- a loop?")
        return values


def affine_form(evaluate, width):
    """Recover (constant, dependency mask) of an affine GF(2) function.

    ``evaluate(state) -> 0/1`` is probed on the zero vector and on the 16 unit
    vectors; that is all an affine function needs.  The caller is responsible
    for checking that the function really is affine.
    """
    zero = [0] * width
    constant = evaluate(zero)
    mask = 0
    for bit in range(width):
        vector = [0] * width
        vector[bit] = 1
        if evaluate(vector) != constant:
            mask |= 1 << bit
    return constant, mask


def is_affine(evaluate, width, constant, mask, samples, rng):
    for _ in range(samples):
        vector = [rng.randint(0, 1) for _ in range(width)]
        expected = constant
        for bit in range(width):
            if (mask >> bit) & 1:
                expected ^= vector[bit]
        if evaluate(vector) != expected:
            return False
    return True


# ---------------------------------------------------------------------------
# the walk
# ---------------------------------------------------------------------------

def analyse(hal_py, netlist, checks, rng, cycles):
    out = {}

    gates = netlist.get_gates()
    types = {}
    for gate in gates:
        name = gate.get_type().get_name()
        types[name] = types.get(name, 0) + 1
    out["gate_types"] = types
    checks("netlist has %d flip-flops" % EXPECTED_FF,
           types.get("tennm_ff") == EXPECTED_FF, "found %s" % types.get("tennm_ff"))
    checks("netlist has %d ALM cells" % EXPECTED_LCELL,
           types.get("tennm_lcell_comb") == EXPECTED_LCELL,
           "found %s" % types.get("tennm_lcell_comb"))
    checks("no primitive outside the modelled coverage",
           set(types) <= {"tennm_ff", "tennm_lcell_comb", "HAL_GND", "HAL_VCC"},
           ", ".join(sorted(types)))

    ffs = sorted([g for g in gates if g.get_type().get_name() == "tennm_ff"],
                 key=lambda g: g.get_id())

    # --- one clock, one enable, one asynchronous clear ----------------------
    def net_id(gate, pin):
        net = gate.get_fan_in_net(pin)
        return net.get_id() if net is not None else None

    clocks = {net_id(g, "clk") for g in ffs}
    enables = {net_id(g, "ena") for g in ffs}
    clears = {net_id(g, "clrn") for g in ffs}
    checks("all flip-flops share one clock net", len(clocks) == 1)
    checks("all flip-flops share one enable net", len(enables) == 1)
    checks("all flip-flops share one asynchronous clear net", len(clears) == 1)
    clear_id = list(clears)[0]
    clear_net = netlist.get_net_by_id(clear_id) if clear_id is not None else None
    checks("the asynchronous clear is a primary input",
           clear_net is not None and clear_net.is_global_input_net(),
           clear_net.get_name() if clear_net is not None else "unconnected")

    evaluator = Evaluator(hal_py, netlist, ffs)
    width = len(ffs)

    # --- next-state functions of every flip-flop ---------------------------
    d_nets = [g.get_fan_in_net("d") for g in ffs]

    def next_state_bit(index):
        net_id = d_nets[index].get_id()

        def evaluate(state):
            return evaluator.run(state)[net_id]
        return evaluate

    rows = []
    affine = True
    for index in range(width):
        evaluate = next_state_bit(index)
        constant, mask = affine_form(evaluate, width)
        if not is_affine(evaluate, width, constant, mask, 32, rng):
            affine = False
        rows.append((constant, mask))
    checks("every next-state function is affine over GF(2) (32 random probes each)",
           affine)
    out["rows"] = rows

    # --- the shift chain ---------------------------------------------------
    single = [i for i, (_, mask) in enumerate(rows) if bin(mask).count("1") == 1]
    multi = [i for i, (_, mask) in enumerate(rows) if bin(mask).count("1") > 1]
    checks("exactly one flip-flop has a wide next-state function",
           len(multi) == 1, "wide: %s, shift-like: %d" % (multi, len(single)))
    if len(multi) != 1:
        raise CheckFailed("no unique feedback stage; the shape is not an LFSR")
    injection = multi[0]

    successor = {}
    for index, (_, mask) in enumerate(rows):
        if bin(mask).count("1") == 1:
            successor[mask.bit_length() - 1] = index
    order = [injection]
    while len(order) < width:
        nxt = successor.get(order[-1])
        if nxt is None or nxt in order:
            break
        order.append(nxt)
    checks("the shift-like edges form one chain of %d stages" % width,
           len(order) == width, "chain length %d" % len(order))
    if len(order) != width:
        raise CheckFailed("the flip-flops do not form a single shift chain")
    stage_of = {gate_index: stage for stage, gate_index in enumerate(order)}
    out["order"] = order

    taps = sorted((stage_of[bit] for bit in range(width)
                   if (rows[injection][1] >> bit) & 1), reverse=True)
    out["taps"] = taps
    checks("the feedback taps are %s" % (list(EXPECTED_TAPS),),
           tuple(taps) == EXPECTED_TAPS, "recovered %s" % (taps,))
    checks("the last stage is one of the taps", (width - 1) in taps)

    # --- polarity: which stages are stored inverted ------------------------
    #  Recovered twice, independently:
    #  (a) from the primary outputs -- an output that is the complement of its
    #      flip-flop means the flip-flop stores the complement of the design
    #      value;
    #  (b) from the shift edges -- an inverting shift edge flips the polarity
    #      between two neighbouring stages.
    outputs = []
    undriven = []
    for net in netlist.get_global_output_nets():
        target = net.get_id()
        if target not in evaluator.run([0] * width):
            undriven.append(net.get_name())
            continue

        def evaluate(state, target=target):
            return evaluator.run(state)[target]
        constant, mask = affine_form(evaluate, width)
        outputs.append((net.get_name(), constant, mask))
    checks("every primary output is driven by the combinational layer",
           not undriven, "not evaluable: %s" % (undriven,))

    polarity_ports = {}
    for name, constant, mask in outputs:
        if bin(mask).count("1") != 1:
            continue
        stage = stage_of[mask.bit_length() - 1]
        polarity_ports.setdefault(stage, constant)
    checks("every primary output is one stage, optionally inverted",
           all(bin(mask).count("1") == 1 for _, _, mask in outputs))
    checks("all %d stages are observable on a primary output" % width,
           len(polarity_ports) == width, "observable: %d" % len(polarity_ports))

    polarity_chain = {0: polarity_ports.get(0, 0)}
    for stage in range(1, width):
        constant, _ = rows[order[stage]]
        polarity_chain[stage] = polarity_chain[stage - 1] ^ constant
    checks("port-derived and chain-derived polarity agree",
           polarity_chain == polarity_ports,
           "chain %s vs ports %s" % (
               "".join(str(polarity_chain[i]) for i in range(width)),
               "".join(str(polarity_ports.get(i, 9)) for i in range(width))))
    out["polarity"] = [polarity_chain[i] for i in range(width)]

    seed = 0
    for stage in range(width):
        seed |= polarity_chain[stage] << stage
    out["seed"] = seed
    checks("the recovered reset seed is 0x%04X" % EXPECTED_SEED,
           seed == EXPECTED_SEED, "recovered 0x%04X" % seed)

    # --- the design-domain transition is purely linear ---------------------
    #  y_k = q_k XOR polarity_k.  If the polarity map is the right one, every
    #  constant in the next-state system disappears.
    linear = True
    for stage in range(width):
        gate_index = order[stage]
        constant, mask = rows[gate_index]
        value = constant ^ polarity_chain[stage]
        for bit in range(width):
            if (mask >> bit) & 1:
                value ^= polarity_chain[stage_of[bit]]
        if value != 0:
            linear = False
    checks("in the de-inverted domain the next-state map has no constant term",
           linear)

    # --- maximal length ----------------------------------------------------
    mask16 = (1 << width) - 1

    def step(state):
        feedback = 0
        for tap in taps:
            feedback ^= (state >> tap) & 1
        return ((state << 1) | feedback) & mask16

    state = seed
    period = 0
    seen = set()
    while True:
        seen.add(state)
        state = step(state)
        period += 1
        if state == seed or period > mask16 + 2:
            break
    out["period"] = period
    checks("the recovered LFSR has period %d (2^16-1)" % EXPECTED_PERIOD,
           period == EXPECTED_PERIOD, "period %d" % period)
    checks("all %d states of the cycle are distinct" % EXPECTED_PERIOD,
           len(seen) == EXPECTED_PERIOD, "%d distinct states" % len(seen))
    checks("the all-zero state is not on the cycle", 0 not in seen)

    # --- gate-level cross-check -------------------------------------------
    #  Start from the netlist's own reset state (every flip-flop cleared) and
    #  step the real gates; the recovered model has to predict every cycle.
    trace = []
    q = [0] * width
    model = seed
    agree = True
    for cycle in range(cycles):
        values = evaluator.run(q)
        observed = 0
        for stage in range(width):
            observed |= (q[order[stage]] ^ polarity_chain[stage]) << stage
        trace.append((cycle, observed, model))
        if observed != model:
            agree = False
            break
        nxt = [values[d_nets[i].get_id()] for i in range(width)]
        q = nxt
        model = step(model)
    checks("gate-level simulation matches the recovered model for %d cycles"
           % cycles, agree)
    out["trace"] = trace
    out["cycles"] = cycles

    print("\n  recovered: %d-stage Fibonacci LFSR, taps %s -> %s,"
          "\n             seed 0x%04X, period %d, stored-inverted stages %s"
          % (width, taps, EXPECTED_POLYNOMIAL, seed, period,
             [s for s in range(width) if polarity_chain[s]]))

    return out


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------

def write_artifacts(out, netlist, out_dir, netlist_path):
    os.makedirs(out_dir, exist_ok=True)
    width = EXPECTED_STAGES
    order = out["order"]
    rows = out["rows"]
    polarity = out["polarity"]
    ffs = sorted([g for g in netlist.get_gates()
                  if g.get_type().get_name() == "tennm_ff"], key=lambda g: g.get_id())

    lines = ["# next-state functions, recovered by probing the netlist",
             "# left: as the netlist computes it (q = what the flip-flop stores)",
             "# right: after undoing the recovered inversion map (y = design value)",
             ""]
    for stage in range(width):
        gate_index = order[stage]
        constant, mask = rows[gate_index]
        netlist_terms = ["q(%s)" % ffs[bit].get_name() for bit in range(width)
                         if (mask >> bit) & 1]
        design_terms = ["y[%d]" % s for s in sorted(
            (i for i in range(width) if (mask >> order[i]) & 1), reverse=True)]
        lines.append("stage %2d  (gate %s)" % (stage, ffs[gate_index].get_name()))
        lines.append("    netlist:  d = %s%s" % (
            "!" if constant else "", " ^ ".join(netlist_terms) or "0"))
        lines.append("    design :  y[%d]' = %s" % (stage, " ^ ".join(design_terms)))
        lines.append("    stored inverted: %s" % ("yes" if polarity[stage] else "no"))
    lines.append("")
    lines.append("taps (0-based stage indices): %s" % (out["taps"],))
    lines.append("polynomial: %s" % EXPECTED_POLYNOMIAL)
    lines.append("seed (from the inversion map): 0x%04X" % out["seed"])
    lines.append("period: %d" % out["period"])
    _write(os.path.join(out_dir, "next_state_functions.txt"), "\n".join(lines) + "\n")

    matrix = ["# GF(2) transition matrix in the de-inverted (design) domain",
              "# row k = which stages feed stage k on the next clock",
              "#      " + "".join("%d" % (i % 10) for i in reversed(range(width)))]
    for stage in range(width):
        gate_index = order[stage]
        _, mask = rows[gate_index]
        bits = ["1" if (mask >> order[j]) & 1 else "." for j in reversed(range(width))]
        matrix.append("y[%2d]  %s" % (stage, "".join(bits)))
    _write(os.path.join(out_dir, "transition_matrix.txt"), "\n".join(matrix) + "\n")

    trace_lines = ["# gate-level simulation of netlist.hal.v after reset (en=1)",
                   "# cycle  netlist state   recovered model",
                   "#        (de-inverted)   (16-bit LFSR)"]
    for cycle, observed, model in out["trace"][:64]:
        trace_lines.append("%6d  0x%04X          0x%04X%s" % (
            cycle, observed, model, "" if observed == model else "   MISMATCH"))
    _write(os.path.join(out_dir, "state_trace.txt"), "\n".join(trace_lines) + "\n")

    dot = recovered_dot(out)
    _write(os.path.join(out_dir, "recovered_lfsr.dot"), dot)
    return out_dir


def recovered_dot(out):
    width = EXPECTED_STAGES
    taps = out["taps"]
    seed = out["seed"]
    lines = [
        "digraph recovered_lfsr {",
        '  graph [rankdir=LR, fontname="Helvetica", labelloc=t, '
        'label="16-bit Fibonacci LFSR recovered from netlist.hal.v\\n'
        'taps {%s}  =  %s   seed 0x%04X   period %d"];'
        % (", ".join(str(t) for t in taps), EXPECTED_POLYNOMIAL, seed, out["period"]),
        '  node [shape=box, style=filled, fontname="Helvetica", fillcolor="#eef3fb", '
        'color="#39598a"];',
        '  edge [color="#39598a"];',
        '  xor [shape=circle, width=0.45, label="XOR", fillcolor="#ffe9c9", color="#b3762a"];',
    ]
    for stage in range(width):
        inverted = out["polarity"][stage]
        lines.append('  s%d [label="y[%d]\\nseed %d%s"%s];' % (
            stage, stage, (seed >> stage) & 1,
            "\\n(stored inverted)" if inverted else "",
            ', fillcolor="#e7f3e7", color="#3d7a3d"' if stage in taps else ""))
    for stage in range(width - 1):
        lines.append("  s%d -> s%d;" % (stage, stage + 1))
    for tap in taps:
        lines.append('  s%d -> xor [style=dashed, color="#b3762a"];' % tap)
    lines.append("  xor -> s0;")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _write(path, text):
    with open(path, "w") as handle:
        handle.write(text)
    print("  wrote " + path)


def findings_document(out, netlist_path, image_paths):
    sys.path.insert(0, os.path.join(REPO, "tools"))
    from hal_findings import model, serialize
    from hal_findings.adapters.common import utc_now
    import hashlib

    with open(netlist_path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    artifact = model.artifact(
        "lfsr_prng_netlist",
        kind="netlist",
        path=os.path.relpath(netlist_path, REPO).replace(os.sep, "/"),
        sha256=digest,
        design_name="lfsr_prng",
        gate_count=sum(out["gate_types"].values()),
        gate_library={"name": "AGILEX_TENNM"},
        description="Quartus Prime Pro Agilex 3 export, rewritten by tools/hal_agilex import",
    )
    common_scope = model.scope(["lfsr_prng_netlist"],
                               description="the 16 tennm_ff stages and the ALM cells between them")
    semantics = model.assumption(
        "agilex-primitive-semantics",
        "tennm_lcell_comb / tennm_ff behave as tools/hal_agilex/primitives.py models "
        "them; that model is itself validated against the vendor export it describes.",
        kind="library",
    )
    linearity = model.assumption(
        "affine-probing",
        "Each next-state function was recovered as an affine GF(2) form from 17 probes "
        "and spot-checked on 32 random states per stage, not proven affine exhaustively.",
        kind="tool",
        discharged=False,
    )
    evidence = [model.evidence("file", description=os.path.basename(p), path=p,
                               media_type="image/svg+xml")
                for p in image_paths]

    findings = [
        model.finding(
            "lfsr/structure/shift-chain",
            "The 16 flip-flops form one shift chain with a single feedback point",
            model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            model.method("next-state function probing", "structural", False,
                         description="Every flip-flop's D cone was evaluated over the "
                                     "flip-flop outputs; 15 stages depend on exactly one "
                                     "predecessor, one stage depends on four."),
            common_scope,
            summary="Stage order %s (indices into the flip-flop list, sorted by gate id); one "
                    "injection point, no other sequential structure." % (out["order"],),
            assumptions=[semantics, linearity],
            bounds_dict=model.unbounded("a statement about the netlist's functions"),
            data={"stage_order_gate_index": out["order"]},
            tags=["lfsr", "structure"],
        ),
        model.finding(
            "lfsr/polynomial",
            "The feedback polynomial is %s" % EXPECTED_POLYNOMIAL,
            model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            model.method("tap extraction from the feedback function", "structural", False,
                         description="The wide next-state function is the XOR of the "
                                     "stages listed as taps."),
            common_scope,
            summary="Taps %s (0-based stage indices), i.e. %s."
                    % (out["taps"], EXPECTED_POLYNOMIAL),
            assumptions=[semantics, linearity],
            bounds_dict=model.unbounded("a statement about the netlist's functions"),
            data={"taps": out["taps"], "polynomial": EXPECTED_POLYNOMIAL},
            evidence_list=evidence,
            tags=["lfsr", "polynomial"],
        ),
        model.finding(
            "lfsr/seed",
            "The reset seed is 0x%04X" % out["seed"],
            model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            model.method("inversion-map recovery", "structural", False,
                         description="The flip-flops only have an asynchronous clear, so a "
                                     "stage whose design value resets to 1 is stored "
                                     "inverted; the set of inverted stages is the seed. "
                                     "Recovered twice: from the primary outputs and from "
                                     "the inverting shift edges."),
            common_scope,
            summary="Both derivations agree on 0x%04X." % out["seed"],
            assumptions=[semantics],
            bounds_dict=model.unbounded("a statement about the netlist's structure"),
            data={"seed": out["seed"], "polarity": out["polarity"]},
            tags=["lfsr", "reset"],
        ),
        model.finding(
            "lfsr/maximal-length",
            "The recovered generator has maximal length (period %d)" % out["period"],
            model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            model.method("state enumeration of the recovered model", "simulation", False,
                         description="The recovered 16-bit model was iterated from the "
                                     "recovered seed until it returned to it; all states on "
                                     "the cycle were collected."),
            common_scope,
            summary="%d distinct states before the seed recurs; the all-zero state is not "
                    "on the cycle." % out["period"],
            assumptions=[semantics, linearity,
                         model.assumption(
                             "model-equals-netlist",
                             "The period was enumerated on the recovered model, not on the "
                             "gate-level netlist; the two were only compared cycle by cycle "
                             "for a bounded run.",
                             kind="environment", discharged=False)],
            bounds_dict=model.unbounded("all 65535 states of the recovered model"),
            data={"period": out["period"]},
            tags=["lfsr", "period"],
        ),
        model.finding(
            "lfsr/gate-level-agreement",
            "Gate-level simulation agrees with the recovered model",
            model.STATUS_PROVEN_BOUNDED,
            model.method("gate-level simulation", "simulation", True,
                         description="The netlist was stepped from its reset state with the "
                                     "enable high and compared against the recovered model."),
            common_scope,
            summary="%d cycles, no divergence." % out["cycles"],
            assumptions=[semantics],
            bounds_dict=model.bounded(out["cycles"], description="simulated cycles"),
            tags=["lfsr", "simulation"],
        ),
    ]
    return serialize.normalize_document(model.document(
        {"name": "examples/agilex3_walkthroughs/05_lfsr_prng/check.py", "version": "1.0.0"},
        [artifact],
        {"entry_point": "check.py:analyse",
         "plugin": {"name": "hal_agilex+hal_py", "version": "1.0.0"}},
        findings,
        generated_at=utc_now(),
        notes=["Every claim here was derived from the netlist; design.v was not read."],
    ))


# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--netlist", default=os.path.join(HERE, "netlist.hal.v"))
    parser.add_argument("--gate-library", default=os.path.join(
        REPO, "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl"))
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--hal-lib", action="append", default=[],
                        help="directory containing hal_py (default: $HAL_PY_PATH)")
    parser.add_argument("--cycles", type=int, default=128,
                        help="gate-level cycles to cross-check (default 128)")
    parser.add_argument("--out-dir", default=os.path.join(HERE, "artifacts"))
    parser.add_argument("--write-artifacts", action="store_true",
                        help="write the text/dot/findings artifacts the guide embeds")
    parser.add_argument("--seed", type=int, default=20260908)
    args = parser.parse_args(argv)
    if not args.hal_lib:
        args.hal_lib = [d for d in os.environ.get("HAL_PY_PATH", "").split(os.pathsep) if d]

    print("netlist:      " + args.netlist)
    print("gate library: " + args.gate_library)
    try:
        hal_py, netlist, report = load(args)
    except Exception as exc:                       # noqa: BLE001 - environment problem
        print("environment not usable: %s" % exc)
        return 2
    print("elaborated %d ALM cells, %d flip-flops checked, %d refused"
          % (report["elaborated"], report["checked_ff"], len(report["refused"])))

    checks = Checks()
    checks("hal_agilex refused no gate", not report["refused"], str(report["refused"]))
    rng = random.Random(args.seed)
    try:
        out = analyse(hal_py, netlist, checks, rng, args.cycles)
    except CheckFailed as exc:
        print("  [FAIL] %s" % exc)
        return 1

    if args.write_artifacts:
        write_artifacts(out, netlist, args.out_dir, args.netlist)
        document = findings_document(
            out, args.netlist,
            ["examples/agilex3_walkthroughs/05_lfsr_prng/images/recovered_lfsr.svg"])
        path = os.path.join(args.out_dir, "lfsr.findings.json")
        with open(path, "w") as handle:
            handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
        print("  wrote " + path)

    failed = checks.failed
    print("\n%d checks, %d failed" % (len(checks.results), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
