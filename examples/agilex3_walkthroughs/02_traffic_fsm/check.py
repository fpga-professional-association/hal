#!/usr/bin/env python3
"""Re-check every load-bearing claim in ``guide.html``, headlessly.

Runs on a plain Python 3 interpreter: no HAL build, no Quartus, no Graphviz.
That is deliberate -- the assertions here are the ones a CI job can afford to
run on every commit, and they are exactly the numbers the guide quotes.

    python examples/agilex3_walkthroughs/02_traffic_fsm/check.py

Exit code 0 when every check passes, 1 otherwise.  Each check prints the claim
it is checking and the value it measured, so a failure says what moved.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from hal_agilex import behavior, primitives, simulate, vo_netlist  # noqa: E402

FAILURES: list[str] = []


def check(claim: str, ok: bool, detail: str = "") -> None:
    status = "ok  " if ok else "FAIL"
    print("[{}] {}{}".format(status, claim, ("  -- " + detail) if detail else ""))
    if not ok:
        FAILURES.append(claim)


def load_reference(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. the netlist is what the guide says it is
# ---------------------------------------------------------------------------


def check_inventory():
    for name in ("traffic_fsm.vo", "netlist.hal.v", "netlist_anon.hal.v"):
        parsed = vo_netlist.parse_file(str(HERE / name))
        ffs = list(parsed.instances_of_type(primitives.FF))
        luts = list(parsed.instances_of_type(primitives.LCELL))
        check("{}: 8 tennm_ff and 14 tennm_lcell_comb".format(name),
              (len(ffs), len(luts)) == (8, 14),
              "got {} ff, {} lcell".format(len(ffs), len(luts)))
        others = {i.type for i in parsed.instances} - {primitives.FF, primitives.LCELL}
        check("{}: no other primitive types".format(name), not others, str(sorted(others)))


# ---------------------------------------------------------------------------
# 2. the anonymization is a pure relabelling
# ---------------------------------------------------------------------------


def check_anonymization():
    out = HERE / "artifacts" / "_regenerated_anon.hal.v"
    proc = subprocess.run(
        [sys.executable, str(HERE / "anonymize.py"), str(HERE / "netlist.hal.v"),
         "-o", str(out)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        check("anonymize.py reruns cleanly", False, proc.stderr.strip()[:200])
        return
    same = out.read_text(encoding="utf-8") == (HERE / "netlist_anon.hal.v").read_text(
        encoding="utf-8")
    check("anonymize.py reproduces netlist_anon.hal.v byte for byte", same)
    out.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. the structural split the whole walkthrough rests on
# ---------------------------------------------------------------------------


def dependency_graph(parsed):
    """Flip-flop dependency graph, built without HAL.

    ``u -> v`` when the ``d`` (or ``ena``) pin of flip-flop ``u`` reaches the
    ``q`` output of flip-flop ``v`` through combinational cells only.
    """
    driver = {}
    for inst in parsed.instances:
        for pin in ("combout", "sumout", "cout", "q"):
            bits = inst.connections.get(pin)
            if bits:
                driver[inst.single(pin).key] = (inst, pin)

    ffs = list(parsed.instances_of_type(primitives.FF))
    ff_by_q = {inst.single("q").key: inst.name for inst in ffs}

    depends = {}
    for ff in ffs:
        seen = set()
        found = set()
        stack = []
        for pin in ("d", "ena"):
            bit = ff.single(pin)
            if bit is not None and hasattr(bit, "key"):
                stack.append(bit.key)
        while stack:
            key = stack.pop()
            if key in seen:
                continue
            seen.add(key)
            if key in ff_by_q:
                found.add(ff_by_q[key])
                continue
            entry = driver.get(key)
            if entry is None:
                continue
            inst, _ = entry
            for pin in primitives.LCELL_INPUT_PINS:
                bit = inst.single(pin)
                if bit is not None and hasattr(bit, "key"):
                    stack.append(bit.key)
        depends[ff.name] = found
    return depends


def sccs(nodes, edges):
    index = {}
    low = {}
    on = set()
    stack = []
    out = []
    counter = [0]

    def strong(v):
        work = [(v, iter(sorted(edges.get(v, ()))))]
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        while work:
            node, it = work[-1]
            pushed = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(sorted(edges.get(nxt, ())))))
                    pushed = True
                    break
                if nxt in on:
                    low[node] = min(low[node], index[nxt])
            if pushed:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    top = stack.pop()
                    on.discard(top)
                    component.append(top)
                    if top == node:
                        break
                out.append(sorted(component))

    for node in sorted(nodes):
        if node not in index:
            strong(node)
    return sorted(out)


def reaches_outputs(parsed):
    """Flip-flops whose q reaches a top-level output net through LUTs only."""
    outputs = {name for name, (direction, _, _) in parsed.declarations.items()
               if direction == "output"}
    output_keys = set()
    for name in outputs:
        for bit in parsed.bits_of(name):
            output_keys.add(bit.key)
    # what each net feeds
    consumers: dict[str, list] = {}
    for inst in parsed.instances:
        if inst.type != primitives.LCELL:
            continue
        for pin in primitives.LCELL_INPUT_PINS:
            bit = inst.single(pin)
            if bit is not None and hasattr(bit, "key"):
                consumers.setdefault(bit.key, []).append(inst)
    alias: dict[str, list[str]] = {}
    for target, source in parsed.assignments:
        for t, s in zip(target, source):
            if hasattr(s, "key") and hasattr(t, "key"):
                alias.setdefault(s.key, []).append(t.key)

    found = []
    for ff in parsed.instances_of_type(primitives.FF):
        stack = [ff.single("q").key]
        seen = set()
        hit = False
        while stack and not hit:
            key = stack.pop()
            if key in seen:
                continue
            seen.add(key)
            if key in output_keys:
                hit = True
                break
            stack.extend(alias.get(key, []))
            for inst in consumers.get(key, []):
                for pin in ("combout", "sumout", "cout"):
                    bit = inst.single(pin)
                    if bit is not None and hasattr(bit, "key"):
                        stack.append(bit.key)
        if hit:
            found.append(ff.name)
    return found


def check_structure():
    parsed = vo_netlist.parse_file(str(HERE / "netlist_anon.hal.v"))
    depends = dependency_graph(parsed)
    components = sccs(list(depends), depends)
    non_trivial = [c for c in components if len(c) > 1]

    # The guide's step 3 makes a point of this: the SCC is the *whole* register
    # file, so it does not separate the controller from its dwell counter.
    check("the flip-flop dependency graph is a single 8-member SCC",
          len(non_trivial) == 1 and len(non_trivial[0]) == 8, str(non_trivial))

    # The split the guide actually uses: the net on the ena pin.
    ffs = list(parsed.instances_of_type(primitives.FF))
    by_enable: dict[str, list[str]] = {}
    for ff in ffs:
        bit = ff.single("ena")
        key = getattr(bit, "key", str(bit))
        by_enable.setdefault(key, []).append(ff.name)
    check("the flip-flops split 4/4 by the net on their ena pin",
          sorted(len(v) for v in by_enable.values()) == [4, 4],
          str({k: sorted(v) for k, v in by_enable.items()}))

    # Both halves are internally mutually dependent -- a synchronous-clear
    # counter is an SCC too -- so "is it an SCC" is not the discriminator.
    for enable, members in sorted(by_enable.items()):
        inner = {m: depends[m] & set(members) for m in members}
        parts = [c for c in sccs(sorted(members), inner) if len(c) > 1]
        check("group ena={} is itself one SCC (so SCC-ness does not separate"
              " counter from controller)".format(enable),
              bool(parts) and len(parts[0]) == len(members), str(parts))

    # The discriminator the guide uses: only the state register reaches the pins.
    reaching = sorted(reaches_outputs(parsed))
    check("exactly 4 flip-flops reach a primary output combinationally",
          len(reaching) == 4, str(reaching))
    always_on = sorted(by_enable.get("vcc", []))
    check("they are the same 4 the enable split calls always-enabled",
          reaching == always_on, "{} vs {}".format(reaching, always_on))


# ---------------------------------------------------------------------------
# 4. the measured behaviour of the netlist
# ---------------------------------------------------------------------------


def released_simulator(parsed):
    """A simulator with reset released and every data input at 0."""
    sim = simulate.build(parsed)
    ffs = list(parsed.instances_of_type(primitives.FF))
    clock = ffs[0].single("clk").key
    reset = ffs[0].single("clrn").key
    ports = [n for n, (direction, _, _) in parsed.declarations.items()
             if direction == "input"]
    data = [p for p in sorted(ports) if p not in (clock, reset)]
    outs = sorted(n for n, (direction, _, _) in parsed.declarations.items()
                  if direction == "output")
    sim.set_input(clock, 0)
    sim.set_input(reset, 0)
    for name in data:
        sim.set_input(name, 0)
    sim.apply_async_clear()
    sim.set_input(reset, 1)
    return sim, outs


def measure_phases():
    parsed = vo_netlist.parse_file(str(HERE / "netlist_anon.hal.v"))
    sim, outs = released_simulator(parsed)

    pattern = []
    for _ in range(80):
        pattern.append(tuple(sim.get_output(o) for o in outs))
        sim.clock()

    phases = []
    last, length = None, 0
    for value in pattern:
        if value != last:
            if last is not None:
                phases.append((last, length))
            last, length = value, 1
        else:
            length += 1
    phases.append((last, length))
    return outs, phases


def check_behaviour():
    outs, phases = measure_phases()
    lengths = [count for _, count in phases[:4]]
    check("the light cycle is 10 / 3 / 13 / 5 clock cycles",
          lengths == [10, 3, 13, 5], str(list(zip([p for p, _ in phases[:4]], lengths))))
    check("the full period is 31 clock cycles", sum(lengths) == 31, str(sum(lengths)))
    patterns = [p for p, _ in phases[:5]]
    check("four distinct output patterns, one of them lit on two pins",
          len(set(patterns)) == 4 and sum(1 for p in set(patterns) if sum(p) == 2) == 1,
          "{} {}".format(outs, sorted(set(patterns))))


def check_counter():
    """Bit order from toggle rates, then the dwell limit of every phase."""
    parsed = vo_netlist.parse_file(str(HERE / "netlist_anon.hal.v"))
    gated = [ff.name for ff in parsed.instances_of_type(primitives.FF)
             if getattr(ff.single("ena"), "key", None) != "vcc"]

    sim, outs = released_simulator(parsed)
    toggles = {f: 0 for f in gated}
    previous = {f: sim.state[f] for f in gated}
    for _ in range(310):
        sim.clock()
        for f in gated:
            if sim.state[f] != previous[f]:
                toggles[f] += 1
            previous[f] = sim.state[f]
    order = sorted(gated, key=lambda f: (-toggles[f], f))
    counts = [toggles[f] for f in order]
    check("the four gated flip-flops have strictly decreasing toggle counts",
          counts == sorted(counts, reverse=True) and len(set(counts)) == 4,
          "{} -> {}".format(order, counts))

    sim, outs = released_simulator(parsed)
    limits = {}
    trace = []
    for _ in range(80):
        value = sum(sim.state[f] << i for i, f in enumerate(order))
        trace.append((tuple(sim.get_output(o) for o in outs), value))
        sim.clock()
    monotone = True
    for (pat_a, val_a), (pat_b, val_b) in zip(trace, trace[1:]):
        if val_b == 0:
            limits[pat_a] = val_a
        elif val_b != val_a + 1:
            monotone = False
    check("decoded in that bit order the counter increments by 1 every cycle",
          monotone, str(trace[:20]))
    check("the four dwell limits are 9, 2, 12 and 4",
          sorted(limits.values()) == [2, 4, 9, 12], str(limits))


# ---------------------------------------------------------------------------
# 5. the reconstruction agrees with the netlist, and with the original RTL
# ---------------------------------------------------------------------------


def check_equivalence():
    export = vo_netlist.parse_file(str(HERE / "traffic_fsm.vo"))
    for name in ("reference_recovered.py", "reference_original.py"):
        reference = load_reference(HERE / "artifacts" / name)
        with redirect_stdout(io.StringIO()):
            result = behavior.run_reference_check(export, reference, cycles=400)
        check("netlist matches {} for 400 cycles".format(name),
              "mismatch" not in result, str(result.get("mismatch", ""))[:200])

    # Exhaustive over every reachable state of both models, both input values.
    original = load_reference(HERE / "artifacts" / "reference_original.py")
    recovered = load_reference(HERE / "artifacts" / "reference_recovered.py")
    seen = set()
    frontier = [(tuple(sorted(original.initial_state().items())),
                 tuple(sorted(recovered.initial_state().items())))]
    mismatch = None
    visited = 0
    while frontier and mismatch is None:
        a_key, b_key = frontier.pop()
        if (a_key, b_key) in seen:
            continue
        seen.add((a_key, b_key))
        visited += 1
        a, b = dict(a_key), dict(b_key)
        for hold in (0, 1):
            values = {"rst_n": 1, "hold": hold}
            if original.outputs(a, values) != recovered.outputs(b, values):
                mismatch = (a, b, values, original.outputs(a, values),
                            recovered.outputs(b, values))
                break
            na = tuple(sorted(original.next_state(a, values).items()))
            nb = tuple(sorted(recovered.next_state(b, values).items()))
            frontier.append((na, nb))
    check("recovered.v and design.v agree on every reachable state, both inputs",
          mismatch is None, "{} state pairs visited; {}".format(visited, mismatch))


def main() -> int:
    print("traffic_fsm walkthrough -- checking the claims in guide.html\n")
    check_inventory()
    check_anonymization()
    check_structure()
    check_behaviour()
    check_counter()
    check_equivalence()
    print()
    if FAILURES:
        print("{} check(s) FAILED:".format(len(FAILURES)))
        for item in FAILURES:
            print("  - " + item)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
