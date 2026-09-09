#!/usr/bin/env python3
"""The reverse-engineering walk of examples/agilex3_walkthroughs/10_crc8_checker.

Every subcommand is one step of guide.html and prints exactly what the guide
quotes.  Nothing here knows the answer: the only inputs are the netlist, the
AGILEX_TENNM gate library and the primitive semantics `hal_agilex` validated
against the vendor export.  The recovered polynomial, the bit order, the reset
value and the meaning of every port are *derived*, and `check.py` re-asserts
them so the walkthrough cannot rot silently.

    export HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib
    python re_walk.py stats
    python re_walk.py ports
    python re_walk.py registers
    python re_walk.py functions
    python re_walk.py polynomial
    python re_walk.py simulate
    python re_walk.py all -o artifacts/

By default it analyses `netlist/netlist.anon.hal.v`, the copy with every design
name stripped.  Pass `--netlist netlist/netlist.hal.v` to run the same walk on
the export as Quartus wrote it and watch how much the names give away.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
GATE_LIBRARY = os.path.join(
    REPO, "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl"
)
DEFAULT_NETLIST = os.path.join(HERE, "netlist", "netlist.anon.hal.v")

sys.path.insert(0, os.path.join(REPO, "tools"))

FF = "tennm_ff"
LCELL = "tennm_lcell_comb"
STRUCTURAL_NETS = {"gnd", "vcc", "devclrn", "devpor", "devoe", "unknown"}


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


class Design(object):
    """A loaded netlist plus the derived facts every step shares."""

    def __init__(self, hal_py, netlist, elaboration):
        self.hal = hal_py
        self.nl = netlist
        self.elaboration = elaboration
        self.ffs = sorted(
            (g for g in netlist.get_gates() if g.get_type().get_name() == FF),
            key=lambda g: g.get_id(),
        )
        self.lcells = sorted(
            (g for g in netlist.get_gates() if g.get_type().get_name() == LCELL),
            key=lambda g: g.get_id(),
        )

    # -- small helpers ----------------------------------------------------

    def is_const(self, net):
        if net is None:
            return None
        if net.is_gnd_net():
            return 0
        if net.is_vcc_net():
            return 1
        return None

    def net_name(self, net):
        return "<none>" if net is None else net.get_name()

    def q_net(self, ff):
        return ff.get_fan_out_net("q")

    def d_net(self, ff):
        return ff.get_fan_in_net("d")


def load(netlist_path, hal_lib=None):
    from hal_agilex import hal_adapter

    libs = []
    for candidate in (hal_lib, os.environ.get("HAL_PY_PATH")):
        if candidate:
            libs.append(candidate)
    hal_py = hal_adapter.import_hal(libs)
    netlist = hal_adapter.load_netlist(hal_py, netlist_path, GATE_LIBRARY)
    elaboration = hal_adapter.elaborate(hal_py, netlist)
    return Design(hal_py, netlist, elaboration)


# ---------------------------------------------------------------------------
# step 1 -- what is in the box
# ---------------------------------------------------------------------------


def collect_stats(design):
    types = {}
    for gate in design.nl.get_gates():
        name = gate.get_type().get_name()
        types[name] = types.get(name, 0) + 1
    nets = [n for n in design.nl.get_nets()]
    return {
        "top_module": design.nl.get_top_module().get_name(),
        "gates": len(design.nl.get_gates()),
        "gate_types": types,
        "nets": len(nets),
        "global_inputs": sorted(n.get_name() for n in design.nl.get_global_input_nets()),
        "global_outputs": sorted(n.get_name() for n in design.nl.get_global_output_nets()),
        "modules": len(design.nl.get_modules()),
        "elaborated_lcells": design.elaboration["elaborated"],
        "checked_ffs": design.elaboration["checked_ff"],
        "refused": design.elaboration["refused"],
    }


def cmd_stats(design, args):
    stats = collect_stats(design)
    print("top module          : %s" % stats["top_module"])
    print("modules             : %d  (1 == flat, no hierarchy survived)" % stats["modules"])
    print("gates               : %d" % stats["gates"])
    for name in sorted(stats["gate_types"]):
        print("    %-20s %d" % (name, stats["gate_types"][name]))
    print("nets                : %d" % stats["nets"])
    print("global inputs       : %s" % ", ".join(stats["global_inputs"]))
    print("global outputs      : %s" % ", ".join(stats["global_outputs"]))
    print("elaborated ALMs     : %d   (Boolean function attached from lut_mask)"
          % stats["elaborated_lcells"])
    print("checked flip-flops  : %d   (pin configuration inside the modelled coverage)"
          % stats["checked_ffs"])
    print("refused gates       : %d" % len(stats["refused"]))
    for entry in stats["refused"]:
        print("    %s (%s): %s" % (entry["gate"], entry["type"], entry["reason"]))
    return stats


# ---------------------------------------------------------------------------
# step 2 -- what each port is for, structurally
# ---------------------------------------------------------------------------


CONTROL_PINS = {"clk": "clock", "clrn": "async clear (active low)", "ena": "clock enable"}


def collect_ports(design):
    roles = {}
    for net in design.nl.get_global_input_nets():
        pins = {}
        for endpoint in net.get_destinations():
            gate = endpoint.get_gate()
            pin = endpoint.get_pin().get_name()
            key = "%s.%s" % (gate.get_type().get_name(), pin)
            pins[key] = pins.get(key, 0) + 1
        roles[net.get_name()] = {
            "drives": pins,
            "role": _classify(pins),
        }
    outputs = {}
    for net in design.nl.get_global_output_nets():
        sources = []
        for endpoint in net.get_sources():
            sources.append("%s.%s" % (endpoint.get_gate().get_name(),
                                      endpoint.get_pin().get_name()))
        outputs[net.get_name()] = sources
    return {"inputs": roles, "outputs": outputs}


def _classify(pins):
    kinds = set()
    for key in pins:
        pin = key.split(".", 1)[1]
        kinds.add(CONTROL_PINS.get(pin, "data"))
    if kinds == {"clock"}:
        return "clock"
    if kinds == {"async clear (active low)"}:
        return "asynchronous reset, active low"
    if kinds == {"clock enable"}:
        return "clock enable"
    if kinds == {"data"}:
        return "data"
    return "mixed: " + ", ".join(sorted(kinds))


def cmd_ports(design, args):
    ports = collect_ports(design)
    print("primary inputs")
    for name in sorted(ports["inputs"]):
        entry = ports["inputs"][name]
        drives = ", ".join("%s x%d" % (k, v) for k, v in sorted(entry["drives"].items()))
        print("    %-10s -> %-52s  => %s" % (name, drives, entry["role"]))
    print("primary outputs")
    for name in sorted(ports["outputs"]):
        print("    %-14s <- %s" % (name, ", ".join(ports["outputs"][name])))
    return ports


# ---------------------------------------------------------------------------
# step 3 -- the state: registers, and how they depend on each other
# ---------------------------------------------------------------------------


def collect_registers(design):
    banks = {}
    rows = []
    for ff in design.ffs:
        key = (
            design.net_name(ff.get_fan_in_net("clk")),
            design.net_name(ff.get_fan_in_net("clrn")),
            design.net_name(ff.get_fan_in_net("ena")),
        )
        banks.setdefault(key, []).append(ff.get_name())
        rows.append(
            {
                "gate": ff.get_name(),
                "clk": key[0],
                "clrn": key[1],
                "ena": key[2],
                "d": design.net_name(design.d_net(ff)),
                "q": design.net_name(design.q_net(ff)),
                "d_is_q_of": _driver_ff(design, design.d_net(ff)),
            }
        )
    return {"rows": rows,
            "banks": [{"clk": k[0], "clrn": k[1], "ena": k[2], "gates": v}
                      for k, v in sorted(banks.items())],
            "sccs": collect_sccs(design)}


def _driver_ff(design, net):
    if net is None:
        return None
    for endpoint in net.get_sources():
        gate = endpoint.get_gate()
        if gate.get_type().get_name() == FF:
            return gate.get_name()
    return None


def collect_sccs(design):
    """Strongly connected components over the gate graph (graph_algorithm)."""
    try:
        from hal_plugins import graph_algorithm
    except ImportError:
        return None

    graph = graph_algorithm.NetlistGraph.from_netlist(design.nl)
    if graph is None:
        return None
    components = graph_algorithm.get_connected_components(graph, True, 2)
    if components is None:
        return None
    out = []
    for component in components:
        gates = graph.get_gates_from_vertices(component)
        out.append(sorted(g.get_name() for g in gates))
    return sorted(out, key=lambda names: (-len(names), names))


def cmd_registers(design, args):
    info = collect_registers(design)
    print("flip-flops (%d), by control-pin signature" % len(info["rows"]))
    for bank in info["banks"]:
        print("    clk=%s clrn=%s ena=%s : %d gates -> %s"
              % (bank["clk"], bank["clrn"], bank["ena"], len(bank["gates"]),
                 " ".join(bank["gates"])))
    print()
    print("%-6s %-8s %-8s %s" % ("gate", "D net", "Q net", "D driven by"))
    for row in sorted(info["rows"], key=lambda r: r["gate"]):
        print("%-6s %-8s %-8s %s"
              % (row["gate"], row["d"], row["q"],
                 row["d_is_q_of"] or "(combinational logic)"))
    print()
    sccs = info["sccs"]
    if sccs is None:
        print("graph_algorithm unavailable: strongly connected components not computed")
    else:
        print("strongly connected components of size >= 2: %d" % len(sccs))
        for component in sccs:
            print("    %2d gates: %s" % (len(component), " ".join(component)))
    return info


# ---------------------------------------------------------------------------
# step 4 -- symbolic next-state functions
# ---------------------------------------------------------------------------


def _boundary_name(design, net):
    return net.get_name()


def net_function(design, net, cache=None):
    """Symbolic function of *net* over primary inputs and flip-flop outputs."""
    if cache is None:
        cache = {}
    key = None if net is None else net.get_id()
    if key in cache:
        return cache[key]

    bf = design.hal.BooleanFunction
    constant = design.is_const(net)
    if constant is not None:
        result = bf.from_string("0b%d" % constant)
    elif net is None:
        result = bf.from_string("0b0")
    else:
        driver = None
        for endpoint in net.get_sources():
            driver = endpoint
            break
        if driver is None or driver.get_gate().get_type().get_name() == FF:
            # a primary input or a register output: this is where we stop
            result = bf.from_string(_boundary_name(design, net))
        else:
            gate = driver.get_gate()
            pin = driver.get_pin().get_name()
            functions = gate.get_boolean_functions()
            if pin not in functions:
                raise RuntimeError(
                    "no Boolean function on %s.%s -- gate outside the modelled "
                    "coverage, refusing to guess" % (gate.get_name(), pin)
                )
            result = functions[pin]
            for variable in list(result.get_variable_names()):
                inner = net_function(design, gate.get_fan_in_net(variable), cache)
                substituted = result.substitute(variable, inner)
                if substituted is None:
                    raise RuntimeError("substitution failed on %s.%s" % (gate.get_name(), variable))
                result = substituted
            simplified = result.simplify()
            if simplified is not None:
                result = simplified
    cache[key] = result
    return result


def truth_table(design, function, variables):
    """Evaluate *function* over every assignment of *variables* (LSB = first)."""
    value = design.hal.BooleanFunction.Value
    table = []
    for index in range(1 << len(variables)):
        inputs = {}
        for position, name in enumerate(variables):
            inputs[name] = value.ONE if (index >> position) & 1 else value.ZERO
        result = function.evaluate(inputs)
        if result is None:
            raise RuntimeError("evaluate() failed")
        table.append(1 if result == value.ONE else 0)
    return table


def affine_form(design, function, variables):
    """Return (constant, [coefficients]) if *function* is affine over GF(2)."""
    table = truth_table(design, function, variables)
    constant = table[0]
    coefficients = [table[1 << i] ^ constant for i in range(len(variables))]
    for index, actual in enumerate(table):
        expected = constant
        for position in range(len(variables)):
            if (index >> position) & 1:
                expected ^= coefficients[position]
        if expected != actual:
            return None
    return constant, coefficients


def collect_functions(design):
    q_names = [design.net_name(design.q_net(ff)) for ff in design.ffs]
    input_names = sorted(n.get_name() for n in design.nl.get_global_input_nets())
    cache = {}
    rows = []
    for ff in design.ffs:
        function = net_function(design, design.d_net(ff), cache)
        support = [v for v in sorted(function.get_variable_names())]
        variables = [v for v in q_names + input_names if v in support]
        affine = affine_form(design, function, variables)
        rows.append(
            {
                "gate": ff.get_name(),
                "q": design.net_name(design.q_net(ff)),
                "expression": str(function),
                "support": variables,
                "affine": affine is not None,
                "terms": ([variables[i] for i, c in enumerate(affine[1]) if c]
                          if affine else None),
                "constant": affine[0] if affine else None,
            }
        )
    return {"q_nets": q_names, "inputs": input_names, "rows": rows}


def cmd_functions(design, args):
    info = collect_functions(design)
    print("next-state function of every flip-flop, in terms of Q nets and primary inputs")
    print("(the ALM lut_masks have been substituted through; `simplify()` did the algebra)")
    print()
    for row in sorted(info["rows"], key=lambda r: r["gate"]):
        print("  %-5s D = %s" % (row["gate"], row["expression"]))
        if row["affine"]:
            terms = row["terms"] or []
            body = " XOR ".join(terms) if terms else "0"
            if row["constant"]:
                body = ("1 XOR " + body) if terms else "1"
            print("        linear over GF(2):  %s' = %s" % (row["q"], body))
        else:
            print("        NOT affine over GF(2) -- this is not a plain XOR network")
        print()
    return info


# ---------------------------------------------------------------------------
# step 5 -- the polynomial
# ---------------------------------------------------------------------------


def recover_lfsr(design):
    """Derive the bit order, the tap set and the polynomial from the functions."""
    functions = collect_functions(design)
    by_q = {row["q"]: row for row in functions["rows"]}
    q_nets = set(functions["q_nets"])

    if not all(row["affine"] for row in functions["rows"]):
        return {"ok": False, "reason": "at least one next-state function is not affine"}

    # 1. the serial data input: the primary input that appears in state logic
    data_inputs = set()
    for row in functions["rows"]:
        for term in row["terms"]:
            if term not in q_nets:
                data_inputs.add(term)
    if len(data_inputs) != 1:
        return {"ok": False, "reason": "expected exactly one data input, found %s"
                                       % sorted(data_inputs)}
    din = data_inputs.pop()

    # 2. the feedback source: the Q that is xored in wherever the data bit is
    tapped = [row for row in functions["rows"] if din in row["terms"]]
    feedback_candidates = set(q_nets)
    for row in tapped:
        feedback_candidates &= set(row["terms"])
    if len(feedback_candidates) != 1:
        return {"ok": False, "reason": "no single feedback register (candidates %s)"
                                       % sorted(feedback_candidates)}
    feedback = feedback_candidates.pop()

    # 3. the shift chain: strip feedback and data, what is left is the predecessor
    predecessor = {}
    for row in functions["rows"]:
        rest = [t for t in row["terms"] if t != din and t != feedback]
        if len(rest) > 1:
            return {"ok": False, "reason": "%s has %d shift sources" % (row["q"], len(rest))}
        predecessor[row["q"]] = rest[0] if rest else None

    starts = [q for q, p in predecessor.items() if p is None]
    if len(starts) != 1:
        return {"ok": False, "reason": "expected one chain start, found %s" % starts}

    successor = {}
    for q, p in predecessor.items():
        if p is not None:
            successor.setdefault(p, []).append(q)
    order = [starts[0]]
    while True:
        nxt = successor.get(order[-1], [])
        if not nxt:
            break
        if len(nxt) > 1:
            return {"ok": False, "reason": "chain branches at %s" % order[-1]}
        order.append(nxt[0])
    if len(order) != len(functions["rows"]):
        return {"ok": False, "reason": "chain covers %d of %d registers"
                                       % (len(order), len(functions["rows"]))}
    if order[-1] != feedback:
        return {"ok": False, "reason": "the feedback register %s is not the end of the chain"
                                       % feedback}

    taps = [index for index, q in enumerate(order) if din in by_q[q]["terms"]]
    polynomial = 0
    for index in taps:
        polynomial |= 1 << index
    return {
        "ok": True,
        "width": len(order),
        "data_input": din,
        "feedback_register": feedback,
        "bit_order": order,             # index i == bit i of the remainder
        "taps": taps,
        "polynomial": polynomial,
        "polynomial_hex": "0x%02X" % polynomial,
        "polynomial_terms": _terms(polynomial, len(order)),
        "inversions": [row["q"] for row in functions["rows"] if row["constant"]],
    }


def _terms(polynomial, width):
    parts = ["x^%d" % width]
    for index in range(width - 1, 0, -1):
        if (polynomial >> index) & 1:
            parts.append("x^%d" % index if index > 1 else "x")
    if polynomial & 1:
        parts.append("1")
    return " + ".join(parts)


def collect_match(design, lfsr):
    """Check whether some output is the 'remainder is zero' flag."""
    cache = {}
    results = []
    variables = list(lfsr["bit_order"])
    for net in design.nl.get_nets():
        driver = None
        for endpoint in net.get_sources():
            driver = endpoint.get_gate()
            break
        if driver is None or driver.get_type().get_name() == FF:
            continue
        # only nets that leave the design: a global output, or a net that no
        # gate reads (which is how an `assign` to a port can survive import)
        leaves = net.is_global_output_net() or not net.get_destinations()
        if not leaves:
            continue
        function = net_function(design, net, cache)
        support = set(function.get_variable_names())
        if not support.issubset(set(variables)):
            continue
        table = truth_table(design, function, variables)
        zero_flag = [1 if index == 0 else 0 for index in range(len(table))]
        results.append(
            {
                "net": net.get_name(),
                "expression": str(function),
                "is_zero_flag": table == zero_flag,
                "is_nonzero_flag": table == [1 - v for v in zero_flag],
            }
        )
    return results


def cmd_polynomial(design, args):
    lfsr = recover_lfsr(design)
    if not lfsr["ok"]:
        print("could not fit an LFSR/CRC model: %s" % lfsr["reason"])
        return lfsr
    print("state width         : %d bits" % lfsr["width"])
    print("serial data input   : %s" % lfsr["data_input"])
    print("feedback register   : %s (the top of the chain)" % lfsr["feedback_register"])
    print("bit order (0 .. %d)  : %s" % (lfsr["width"] - 1, " ".join(lfsr["bit_order"])))
    print("tapped bit positions: %s" % ", ".join(str(t) for t in lfsr["taps"]))
    print("polynomial          : %s   (%s)" % (lfsr["polynomial_hex"],
                                               lfsr["polynomial_terms"]))
    print("inverted stages     : %s" % (", ".join(lfsr["inversions"]) or "none"))
    print()
    for entry in collect_match(design, lfsr):
        verdict = ("== 0 flag" if entry["is_zero_flag"] else
                   ("!= 0 flag" if entry["is_nonzero_flag"] else "neither"))
        print("output %-9s = %s" % (entry["net"], entry["expression"]))
        print("        over the recovered state word this is exactly the %s" % verdict)
    lfsr["outputs"] = collect_match(design, lfsr)
    return lfsr


# ---------------------------------------------------------------------------
# step 6 -- simulate the netlist on a real codeword
# ---------------------------------------------------------------------------


class NetlistSim(object):
    """Two-valued cycle simulator over the gates' own Boolean functions."""

    def __init__(self, design):
        self.design = design
        self.value = design.hal.BooleanFunction.Value
        self.state = {ff.get_name(): 0 for ff in design.ffs}

    def _net(self, net, values):
        constant = self.design.is_const(net)
        if constant is not None:
            return constant
        if net is None:
            return 0
        name = net.get_name()
        if name in values:
            return values[name]
        driver = None
        for endpoint in net.get_sources():
            driver = endpoint
            break
        if driver is None:
            raise RuntimeError("net %s has no driver and no value" % name)
        gate = driver.get_gate()
        if gate.get_type().get_name() == FF:
            values[name] = self.state[gate.get_name()]
            return values[name]
        pin = driver.get_pin().get_name()
        function = gate.get_boolean_functions()[pin]
        inputs = {}
        for variable in function.get_variable_names():
            bit = self._net(gate.get_fan_in_net(variable), values)
            inputs[variable] = self.value.ONE if bit else self.value.ZERO
        result = function.evaluate(inputs)
        values[name] = 1 if result == self.value.ONE else 0
        return values[name]

    def reset(self):
        # clrn is active low and asynchronous: holding it low forces every
        # modelled flip-flop to 0.  That is the primitive's semantics, not a
        # guess about the design.
        self.state = {ff.get_name(): 0 for ff in self.design.ffs}

    def step(self, primary):
        values = dict(primary)
        nxt = {}
        for ff in self.design.ffs:
            enable = self._net(ff.get_fan_in_net("ena"), values)
            clear = self._net(ff.get_fan_in_net("clrn"), values)
            data = self._net(self.design.d_net(ff), values)
            current = self.state[ff.get_name()]
            if not clear:
                nxt[ff.get_name()] = 0
            elif enable:
                nxt[ff.get_name()] = data
            else:
                nxt[ff.get_name()] = current
        self.state = nxt

    def read(self, net_name):
        values = {}
        net = None
        for candidate in self.design.nl.get_nets():
            if candidate.get_name() == net_name:
                net = candidate
                break
        return self._net(net, values)

    def word(self, bit_order):
        values = {}
        result = 0
        for index, name in enumerate(bit_order):
            net = None
            for candidate in self.design.nl.get_nets():
                if candidate.get_name() == name:
                    net = candidate
                    break
            result |= self._net(net, values) << index
        return result


MESSAGE = b"123456789"


def simulate_codeword(design, lfsr, message=MESSAGE, appended=None, flip=None):
    sim = NetlistSim(design)
    sim.reset()
    ports = collect_ports(design)
    din = lfsr["data_input"]

    # Drive every primary input explicitly: the enable and the reset are held in
    # their "run" state, the clock net is unused (the flip-flop model steps
    # itself), and the data input carries the bit stream.
    base = {}
    for name, entry in ports["inputs"].items():
        if entry["role"] == "clock enable":
            base[name] = 1
        elif entry["role"] == "asynchronous reset, active low":
            base[name] = 1
        else:
            base[name] = 0

    bits = []
    for byte in message:
        for position in range(7, -1, -1):
            bits.append((byte >> position) & 1)
    if appended is not None:
        for position in range(7, -1, -1):
            bits.append((appended >> position) & 1)
    if flip is not None:
        bits[flip] ^= 1

    trace = []
    for bit in bits:
        primary = dict(base)
        primary[din] = bit
        sim.step(primary)
        trace.append(sim.word(lfsr["bit_order"]))
    return {"bits": len(bits), "remainder": sim.word(lfsr["bit_order"]), "trace": trace,
            "sim": sim}


def cmd_simulate(design, args):
    lfsr = recover_lfsr(design)
    if not lfsr["ok"]:
        print("cannot simulate: %s" % lfsr["reason"])
        return lfsr

    ports = collect_ports(design)
    flag_nets = [entry["net"] for entry in collect_match(design, lfsr)
                 if entry["is_zero_flag"]]

    message = MESSAGE
    run = simulate_codeword(design, lfsr, message)
    crc = run["remainder"]
    print("shifted the ASCII message %r through the netlist, %d bits, enable high"
          % (message.decode(), run["bits"]))
    print("remainder after the message      : 0x%02X" % crc)
    print("that is the CRC the netlist computes; a CRC-8/SMBUS reference gives 0x%02X"
          % reference_crc(message, lfsr["polynomial"]))

    good = simulate_codeword(design, lfsr, message, appended=crc)
    print()
    print("now append it and shift the whole codeword in:")
    print("remainder after message || crc   : 0x%02X" % good["remainder"])
    for name in flag_nets:
        print("output %-9s                 : %d" % (name, good["sim"].read(name)))

    bad = simulate_codeword(design, lfsr, message, appended=crc, flip=17)
    print()
    print("flip a single bit of that codeword (bit 17):")
    print("remainder                        : 0x%02X" % bad["remainder"])
    for name in flag_nets:
        print("output %-9s                 : %d" % (name, bad["sim"].read(name)))

    return {
        "message": message.decode(),
        "crc": crc,
        "reference_crc": reference_crc(message, lfsr["polynomial"]),
        "codeword_remainder": good["remainder"],
        "codeword_flag": {n: good["sim"].read(n) for n in flag_nets},
        "corrupted_remainder": bad["remainder"],
        "corrupted_flag": {n: bad["sim"].read(n) for n in flag_nets},
    }


def reference_crc(message, polynomial, width=8):
    """Textbook CRC with the recovered polynomial -- an independent second opinion."""
    top = 1 << (width - 1)
    mask = (1 << width) - 1
    crc = 0
    for byte in message:
        for position in range(7, -1, -1):
            bit = (byte >> position) & 1
            feedback = ((crc >> (width - 1)) & 1) ^ bit
            crc = (crc << 1) & mask
            if feedback:
                crc ^= polynomial
    return crc


# ---------------------------------------------------------------------------
# a picture of what was recovered
# ---------------------------------------------------------------------------


def lfsr_dot(design, lfsr):
    lines = [
        "digraph recovered_lfsr {",
        '  rankdir=LR;',
        '  graph [fontname="Helvetica", labelloc=t, '
        'label="recovered: %d-bit CRC LFSR, polynomial %s\\n%s"];'
        % (lfsr["width"], lfsr["polynomial_hex"], lfsr["polynomial_terms"]),
        '  node [fontname="Helvetica", fontsize=10];',
        '  edge [fontname="Helvetica", fontsize=9];',
        '  din [shape=invhouse, style=filled, fillcolor="#ffe9b0", label="%s\\n(serial data in)"];'
        % lfsr["data_input"],
        '  fb [shape=circle, width=0.32, style=filled, fillcolor="#ffd0d0", label="+"];',
    ]
    for index, q in enumerate(lfsr["bit_order"]):
        fill = "#d8ecff" if index in lfsr["taps"] else "#eeeeee"
        lines.append('  b%d [shape=box, style="filled,rounded", fillcolor="%s", '
                     'label="bit %d\\n%s"];' % (index, fill, index, q))
    for index in range(lfsr["width"] - 1):
        if (index + 1) in lfsr["taps"]:
            lines.append('  x%d [shape=circle, width=0.28, style=filled, '
                         'fillcolor="#ffd0d0", label="+"];' % (index + 1))
            lines.append("  b%d -> x%d;" % (index, index + 1))
            lines.append("  x%d -> b%d;" % (index + 1, index + 1))
            lines.append('  fb -> x%d [color="#c04040"];' % (index + 1))
        else:
            lines.append("  b%d -> b%d;" % (index, index + 1))
    if 0 in lfsr["taps"]:
        lines.append('  fb -> b0 [color="#c04040"];')
    lines.append('  din -> fb;')
    lines.append('  b%d -> fb [color="#c04040", label="feedback"];'
                 % (lfsr["width"] - 1))
    for entry in lfsr.get("outputs", []):
        if entry["is_zero_flag"]:
            lines.append('  zero [shape=house, style=filled, fillcolor="#d6f5d6", '
                         'label="%s\\n(state == 0)"];' % entry["net"])
            for index in range(lfsr["width"]):
                lines.append('  b%d -> zero [style=dotted, color="#60a060", arrowsize=0.5];'
                             % index)
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


COMMANDS = {
    "stats": cmd_stats,
    "ports": cmd_ports,
    "registers": cmd_registers,
    "functions": cmd_functions,
    "polynomial": cmd_polynomial,
    "simulate": cmd_simulate,
}


def cmd_all(design, args):
    results = {}
    for name in ("stats", "ports", "registers", "functions", "polynomial", "simulate"):
        print("=" * 78)
        print("== %s" % name)
        print("=" * 78)
        results[name] = COMMANDS[name](design, args)
        print()
    lfsr = results["polynomial"]
    if args.output:
        os.makedirs(args.output, exist_ok=True)
        if lfsr.get("ok"):
            dot_path = os.path.join(args.output, "recovered_lfsr.dot")
            with open(dot_path, "w") as handle:
                handle.write(lfsr_dot(design, lfsr))
            print("wrote %s" % dot_path)
        json_path = os.path.join(args.output, "re_walk.json")
        with open(json_path, "w") as handle:
            json.dump(_jsonable(results), handle, indent=2, sort_keys=True)
        print("wrote %s" % json_path)
    return results


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if k != "sim"}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode()
    return obj


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=sorted(COMMANDS) + ["all"])
    parser.add_argument("--netlist", default=DEFAULT_NETLIST)
    parser.add_argument("--hal-lib", default=None)
    parser.add_argument("-o", "--output", default=None,
                        help="directory for the artifacts of `all`")
    args = parser.parse_args(argv)

    design = load(args.netlist, args.hal_lib)
    if args.command == "all":
        cmd_all(design, args)
    else:
        COMMANDS[args.command](design, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
