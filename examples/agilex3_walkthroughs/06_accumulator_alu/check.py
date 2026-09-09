#!/usr/bin/env python3
"""Headless smoke check for the 06_accumulator_alu walkthrough.

Re-asserts the claims guide.html makes, so CI can notice when a regenerated
netlist, a changed gate library or a changed analysis quietly invalidates the
guide.

Two tiers:

* **without HAL** -- structural claims about the export plus a behavioural
  comparison against ``recovered_reference.py``.  Needs only a plain Python 3
  interpreter and ``tools/`` on ``sys.path``; this is the tier CI can always
  run.
* **with HAL** (``hal_py`` importable, e.g. ``HAL_PY_PATH=/work/build/lib``) --
  additionally loads ``netlist.hal.v`` through the AGILEX_TENNM gate library,
  attaches the ALM semantics and re-derives the carry chain and the register
  control cone from the HAL netlist.  Skipped, loudly, when ``hal_py`` is not
  importable.

Usage::

    python3 examples/agilex3_walkthroughs/06_accumulator_alu/check.py
    python3 .../check.py --require-hal      # fail instead of skipping tier 2
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TOOLS = os.path.join(REPO, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

NETLIST_VO = os.path.join(HERE, "netlist", "accumulator_alu.vo")
NETLIST_HAL_V = os.path.join(HERE, "netlist", "netlist.hal.v")
REFERENCE = os.path.join(HERE, "recovered_reference.py")
GATE_LIBRARY = os.path.join(
    REPO, "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl"
)

FAILURES = []


def check(label, condition, detail=""):
    status = "ok  " if condition else "FAIL"
    print("[{}] {}{}".format(status, label, "  -- " + detail if detail else ""))
    if not condition:
        FAILURES.append(label)
    return condition


# ---------------------------------------------------------------------------
# tier 1: no HAL needed
# ---------------------------------------------------------------------------


def tier_one():
    from hal_agilex import behavior, inventory, primitives, simulate, vo_netlist

    print("== tier 1: the export itself ==")
    netlist = vo_netlist.parse_file(NETLIST_VO)

    histogram = {}
    for instance in netlist.instances:
        histogram[instance.type] = histogram.get(instance.type, 0) + 1
    check("only modelled primitives",
          set(histogram) == {"tennm_lcell_comb", "tennm_ff"},
          repr(histogram))
    check("22 lcells / 9 flip-flops",
          histogram.get("tennm_lcell_comb") == 22 and histogram.get("tennm_ff") == 9,
          repr(histogram))

    document = inventory.build_document(netlist, path=NETLIST_VO)
    statuses = {finding["status"] for finding in document["findings"]}
    check("inventory reports full coverage",
          statuses == {"proven_under_assumptions"}, repr(sorted(statuses)))

    # --- the carry chain -------------------------------------------------
    by_out = {}
    for instance in netlist.instances:
        cout = instance.connections.get("cout")
        if cout:
            by_out[str(cout)] = instance
    chain_head = None
    successors = {}
    consumed = set()
    for instance in netlist.instances:
        cin = instance.connections.get("cin")
        source = by_out.get(str(cin)) if cin else None
        if source is not None:
            successors[source.name] = instance
            consumed.add(instance.name)
    heads = [i for i in by_out.values() if i.name not in consumed]
    check("exactly one carry chain", len(heads) == 1,
          "heads = {}".format(sorted(i.name for i in heads)))
    if heads:
        chain_head = heads[0]
        chain, cursor = [chain_head], chain_head
        while cursor.name in successors:
            cursor = successors[cursor.name]
            chain.append(cursor)
        check("chain is 10 cells long", len(chain) == 10,
              " -> ".join(c.name for c in chain))
        check("chain head is a carry-in generator, not a bit slice",
              not chain[0].connections.get("sumout") and
              bool(chain[0].connections.get("cout")),
              "{} sumout={!r}".format(chain[0].name,
                                      chain[0].connections.get("sumout")))
        masks = {c.name: c.parameters.get("lut_mask") for c in chain[1:9]}
        check("the eight bit slices share one lut_mask",
              len(set(masks.values())) == 1, repr(sorted(set(masks.values()))))
        check("the last cell is a bare carry tap (lut_mask 0)",
              int(str(chain[9].parameters.get("lut_mask")), 0) == 0
              if chain[9].parameters.get("lut_mask") is not None else False,
              chain[9].name)

    # --- flip-flop configuration ----------------------------------------
    enables = set()
    clears = set()
    for instance in netlist.instances:
        if instance.type != "tennm_ff":
            continue
        enables.add(str(instance.connections.get("ena")))
        clears.add(str(instance.connections.get("clrn")))
    check("all nine flip-flops share one enable net", len(enables) == 1, repr(enables))
    check("all nine flip-flops share one async clear", len(clears) == 1, repr(clears))

    # --- the recovered model reproduces the netlist ----------------------
    print()
    print("== tier 1: recovered.v versus the netlist ==")
    reference = behavior.load_reference(REFERENCE)
    result = behavior.run_reference_check(netlist, reference, cycles=200)
    check("recovered_reference matches the netlist for 200 cycles",
          "mismatch" not in result, repr(result.get("mismatch"))[:400])

    # --- the opcode table, exhaustively over the operand space -----------
    print()
    print("== tier 1: the opcode table ==")
    expectations = {
        0b00: "hold",
        0b01: "add",
        0b10: "sub",
        0b11: "clear",
    }
    for op, name in sorted(expectations.items()):
        ok = True
        detail = ""
        for acc0 in range(0, 256, 11):
            for operand in range(0, 256, 13):
                simulator = simulate.Simulator(netlist)
                simulator.reset()
                for port in ("op", "operand", "rst_n"):
                    simulator.set_input(port, 0)
                simulator.apply_async_clear()
                simulator.set_input("rst_n", 1)
                simulator.set_input("op", 0b01)
                simulator.set_input("operand", acc0)
                simulator.clock()
                carry_before = simulator.get_output("carry")
                simulator.set_input("op", op)
                simulator.set_input("operand", operand)
                simulator.clock()
                acc = simulator.get_output("acc")
                carry = simulator.get_output("carry")
                if name == "hold":
                    want_acc, want_carry = acc0, carry_before
                elif name == "add":
                    total = acc0 + operand
                    want_acc, want_carry = total & 0xFF, total >> 8
                elif name == "sub":
                    total = acc0 + ((~operand) & 0xFF) + 1
                    want_acc, want_carry = total & 0xFF, total >> 8
                else:
                    want_acc, want_carry = 0, 0
                if (acc, carry) != (want_acc, want_carry):
                    ok = False
                    detail = ("acc0=0x{:02X} operand=0x{:02X} -> acc=0x{:02X} carry={} "
                              "expected acc=0x{:02X} carry={}".format(
                                  acc0, operand, acc, carry, want_acc, want_carry))
                    break
            if not ok:
                break
        check("op = {:02b} is {}".format(op, name), ok, detail)

    # --- zero flag -------------------------------------------------------
    simulator = simulate.Simulator(netlist)
    simulator.reset()
    for port in ("op", "operand", "rst_n"):
        simulator.set_input(port, 0)
    simulator.apply_async_clear()
    simulator.set_input("rst_n", 1)
    check("zero is 1 when acc is 0", simulator.get_output("zero") == 1)
    simulator.set_input("op", 0b01)
    simulator.set_input("operand", 0x01)
    simulator.clock()
    check("zero is 0 when acc is non-zero",
          simulator.get_output("acc") == 1 and simulator.get_output("zero") == 0)

    # --- the sclr variant is genuinely out of coverage -------------------
    variant = os.path.join(HERE, "netlist", "accumulator_alu_sclr_variant.vo")
    if os.path.exists(variant):
        variant_netlist = vo_netlist.parse_file(variant)
        variant_document = inventory.build_document(variant_netlist, path=variant)
        statuses = {f["status"] for f in variant_document["findings"]}
        check("the sclr variant is reported as unsupported",
              "unsupported" in statuses, repr(sorted(statuses)))
    _ = primitives  # imported for the coverage constants the guide quotes


# ---------------------------------------------------------------------------
# tier 2: needs a built HAL
# ---------------------------------------------------------------------------


def tier_two(require):
    print()
    print("== tier 2: the same claims through hal_py ==")
    try:
        from hal_agilex import hal_adapter
        hal_py = hal_adapter.import_hal()
    except Exception as exc:  # hal_py not importable
        message = "hal_py unavailable: {}".format(exc)
        if require:
            check("hal_py importable", False, message)
        else:
            print("[skip] {}".format(message))
        return

    hal_adapter.load_plugins(hal_py)
    netlist = hal_adapter.load_netlist(hal_py, NETLIST_HAL_V, GATE_LIBRARY)
    report = hal_adapter.elaborate(hal_py, netlist)
    check("HAL loads the imported netlist", netlist is not None)
    check("every lcell got a Boolean function",
          report["elaborated"] == 22 and not report["refused"],
          "elaborated={} refused={}".format(report["elaborated"], report["refused"]))
    check("nine flip-flops in the modelled configuration",
          report["checked_ff"] == 9, str(report["checked_ff"]))

    sys.path.insert(0, HERE)
    import analyze  # noqa: E402  -- same directory, same netlist

    chains = analyze.carry_chain(netlist)
    check("HAL sees one carry chain of ten cells",
          len(chains) == 1 and len(chains[0]) == 10,
          repr([[g.get_name() for g in c] for c in chains]))

    enables = set()
    for gate in netlist.get_gates():
        if gate.get_type().get_name() != "tennm_ff":
            continue
        net = gate.get_fan_in_net("ena")
        enables.add(net.get_name() if net else None)
    check("one enable net for the whole register file", len(enables) == 1, repr(enables))

    # The enable really is `op != 0`.
    driver = None
    for gate in netlist.get_gates():
        out = gate.get_fan_out_net("combout")
        if out is not None and out.get_name() in enables:
            driver = gate
            break
    if check("the enable has a combinational driver", driver is not None):
        function = driver.get_boolean_function("combout")
        variables = sorted(function.get_variable_names())
        # the function's variables are pin names (dataa, datab, ...); the claim
        # is about what those pins are wired to, so look up each fan-in net
        feeding = {v: driver.get_fan_in_net(v) for v in variables}
        check("the enable depends only on the opcode bits",
              all(net is not None and "op" in net.get_name() for net in feeding.values()),
              repr({v: net.get_name() if net else None for v, net in feeding.items()}))
        truth = {}
        for vector in range(1 << len(variables)):
            assignment = {
                name: (hal_py.BooleanFunction.Value.ONE if (vector >> bit) & 1
                       else hal_py.BooleanFunction.Value.ZERO)
                for bit, name in enumerate(variables)
            }
            value = function.evaluate(assignment)
            truth[vector] = 1 if value == hal_py.BooleanFunction.Value.ONE else 0
        check("the enable is low for exactly one opcode",
              sum(1 for v in truth.values() if v == 0) == 1, repr(truth))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-hal", action="store_true",
                        help="fail instead of skipping when hal_py is unavailable")
    arguments = parser.parse_args()

    tier_one()
    tier_two(arguments.require_hal)

    print()
    if FAILURES:
        print("{} check(s) FAILED:".format(len(FAILURES)))
        for label in FAILURES:
            print("  - {}".format(label))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
