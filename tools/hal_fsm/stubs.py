"""A netlist and a ``solve_fsm`` that behave like HAL's, without HAL.

The point of this module is not convenience, it is coverage.  Two things in
``hal_fsm`` are easy to get subtly wrong and impossible to test on a machine
that cannot build HAL:

* the backward cone walk and the control-pin classification, which depend on the
  exact accessor chain the bindings expose;
* the state encoding -- in particular that ``solve_fsm`` numbers a state by
  ``state_reg[i]`` but builds its ``initial_state`` argument the other way round
  (``plugins/solve_fsm/src/solve_fsm.cpp``).

:class:`StubSolveFsm` reproduces *both* conventions, including the reversed
initial-state encoding, so a change to
:func:`hal_fsm.transitions.initial_state_argument` that breaks the compensation
fails a test here rather than in a container three days later.

:func:`fixture_netlist` builds the same design as
``tests/fixtures/fsm_controller/controller.v``, gate for gate, so the unit tests
and the headless smoke test check the same machine against the same
``ground_truth.json``.
"""

import os

__all__ = [
    "Pin",
    "GateType",
    "Net",
    "Endpoint",
    "Gate",
    "Netlist",
    "Value",
    "BooleanFunction",
    "HalPy",
    "StubFunction",
    "StubSolveFsm",
    "fixture_netlist",
    "controller_model",
    "counter_model",
]


class Pin(object):
    def __init__(self, name, direction, pin_type="none"):
        self._name = name
        self._direction = direction
        self._type = pin_type

    def get_name(self):
        return self._name

    def get_direction(self):
        return self._direction

    def get_type(self):
        return self._type


class GateType(object):
    def __init__(self, name, properties, pins):
        self._name = name
        self._properties = list(properties)
        self._pins = list(pins)

    def get_name(self):
        return self._name

    def get_property_list(self):
        return list(self._properties)

    def get_pins(self, filter=None):  # noqa: A002 - mirrors the binding's name
        return [pin for pin in self._pins if filter is None or filter(pin)]


class Endpoint(object):
    def __init__(self, gate, pin):
        self._gate = gate
        self._pin = pin

    def get_gate(self):
        return self._gate

    def get_pin(self):
        return self._pin


class Net(object):
    def __init__(self, net_id, name):
        self._id = net_id
        self._name = name
        self._sources = []
        self._destinations = []

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_sources(self):
        return list(self._sources)

    def get_destinations(self):
        return list(self._destinations)


class Gate(object):
    def __init__(self, gate_id, name, gate_type, data=None):
        self._id = gate_id
        self._name = name
        self._type = gate_type
        self._fan_in = {}
        self._fan_out = {}
        self._data = dict(data or {})

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_type(self):
        return self._type

    def get_module(self):
        return None

    def get_data(self, category, key):
        return self._data.get((category, key))

    def connect(self, pin_name, net, direction):
        if direction == "input":
            self._fan_in[pin_name] = net
            net._destinations.append(Endpoint(self, pin_name))
        else:
            self._fan_out[pin_name] = net
            net._sources.append(Endpoint(self, pin_name))

    def get_fan_in_net(self, pin_name):
        return self._fan_in.get(pin_name)

    def get_fan_in_nets(self, filter=None):  # noqa: A002
        nets = list(self._fan_in.values())
        return [net for net in nets if filter is None or filter(net)]

    def get_fan_out_net(self, pin_name):
        return self._fan_out.get(pin_name)

    def get_fan_out_nets(self, filter=None):  # noqa: A002
        nets = list(self._fan_out.values())
        return [net for net in nets if filter is None or filter(net)]


class Netlist(object):
    def __init__(self, design_name="stub", input_filename=""):
        self._gates = []
        self._nets = []
        self._design_name = design_name
        self._input_filename = input_filename
        self._next_gate_id = 1
        self._next_net_id = 1

    # -- construction ----------------------------------------------------

    def net(self, name):
        net = Net(self._next_net_id, name)
        self._next_net_id += 1
        self._nets.append(net)
        return net

    def gate(self, name, gate_type, connections, data=None):
        gate = Gate(self._next_gate_id, name, gate_type, data=data)
        self._next_gate_id += 1
        for pin in gate_type.get_pins():
            net = connections.get(pin.get_name())
            if net is not None:
                gate.connect(pin.get_name(), net, pin.get_direction())
        self._gates.append(gate)
        return gate

    # -- the accessors hal_fsm uses --------------------------------------

    def get_gates(self, filter=None):  # noqa: A002
        return [gate for gate in self._gates if filter is None or filter(gate)]

    def get_nets(self, filter=None):  # noqa: A002
        return [net for net in self._nets if filter is None or filter(net)]

    def get_design_name(self):
        return self._design_name

    def get_device_name(self):
        return ""

    def get_id(self):
        return 1

    def get_input_filename(self):
        return self._input_filename

    def get_gate_library(self):
        return None


# ---------------------------------------------------------------------------
# a hal_py stand-in, only as far as hal_fsm touches it
# ---------------------------------------------------------------------------


class Value(object):
    ZERO = 0
    ONE = 1
    X = None


class BooleanFunction(object):
    Value = Value


class HalPy(object):
    BooleanFunction = BooleanFunction


class StubFunction(object):
    """A condition: variable names, a truth table, and a printable form."""

    def __init__(self, variables, satisfying, text):
        self._variables = sorted(variables)
        #: set of tuples of bits, in ``self._variables`` order
        self._satisfying = {tuple(entry) for entry in satisfying}
        self._text = text

    def get_variable_names(self):
        return set(self._variables)

    def to_string(self):
        return self._text

    def __str__(self):
        return self._text

    def evaluate(self, inputs):
        key = []
        for name in self._variables:
            value = inputs.get(name)
            if value is None:
                return Value.X
            key.append(1 if value == Value.ONE else 0)
        return Value.ONE if tuple(key) in self._satisfying else Value.ZERO


class StubSolveFsm(object):
    """``solve_fsm`` with HAL's conventions, driven by a Python next-state model.

    ``models`` maps ``frozenset`` of flip-flop names to
    ``(input_variables, next_state)`` where ``next_state(state_value, assignment)``
    returns the successor state value under the *transition-table* encoding
    (bit ``i`` = ``state_reg[i]``).  An unknown register set returns ``None``,
    which is exactly what the real plugin does when it cannot solve.
    """

    def __init__(self, models, fail=(), reversed_initial_state=True):
        self.models = dict(models)
        self.fail = set(fail)
        #: reproduce solve_fsm.cpp's reversed initial_state encoding
        self.reversed_initial_state = reversed_initial_state
        self.calls = []

    # -- the three bindings ----------------------------------------------

    def solve_fsm(
        self, nl, state_reg, transition_logic, initial_state=None, graph_path="", timeout=600000
    ):
        names = tuple(gate.get_name() for gate in state_reg)
        self.calls.append(("solve_fsm", names, timeout))
        model = self._model(names)
        if model is None:
            return None
        variables, next_state = model
        width = len(state_reg)

        start = 0
        if initial_state:
            if self.reversed_initial_state:
                for gate in state_reg:
                    start = (start << 1) | (1 if initial_state[gate] else 0)
            else:
                for index, gate in enumerate(state_reg):
                    if initial_state[gate]:
                        start |= 1 << index

        transitions = {}
        queue = [start]
        seen = set()
        while queue:
            state = queue.pop(0)
            if state in seen:
                continue
            seen.add(state)
            for assignment in _assignments(variables):
                successor = next_state(state, assignment)
                transitions.setdefault(state, {}).setdefault(successor, []).append(assignment)
                queue.append(successor)
        return self._to_functions(transitions, variables, graph_path)

    def solve_fsm_brute_force(self, nl, state_reg, transition_logic, graph_path=""):
        names = tuple(gate.get_name() for gate in state_reg)
        self.calls.append(("solve_fsm_brute_force", names, None))
        model = self._model(names)
        if model is None:
            return None
        variables, next_state = model
        transitions = {}
        for state in range(1 << len(state_reg)):
            for assignment in _assignments(variables):
                successor = next_state(state, assignment)
                transitions.setdefault(state, {}).setdefault(successor, []).append(assignment)
        return self._to_functions(transitions, variables, graph_path)

    # -- helpers ---------------------------------------------------------

    def _model(self, names):
        if frozenset(names) in self.fail:
            return None
        return self.models.get(frozenset(names))

    def _to_functions(self, transitions, variables, graph_path):
        out = {}
        for source, successors in transitions.items():
            out[source] = {}
            for target, assignments in successors.items():
                satisfying = {
                    tuple(assignment[name] for name in sorted(variables))
                    for assignment in assignments
                }
                # solve_fsm simplifies every condition before returning it, so a
                # variable the condition does not actually depend on must not
                # show up here either -- otherwise a witness would report inputs
                # that do not matter.
                support = _support(sorted(variables), satisfying)
                indices = [sorted(variables).index(name) for name in support]
                projected = {tuple(entry[index] for index in indices) for entry in satisfying}
                if not projected:
                    text = "0b0"
                elif not support:
                    text = "0b1"
                else:
                    text = " | ".join(
                        "&".join(
                            ("" if entry[position] else "!") + name
                            for position, name in enumerate(support)
                        )
                        for entry in sorted(projected)
                    )
                out[source][target] = StubFunction(support, projected, text)
        if graph_path:
            parent = os.path.dirname(os.path.abspath(graph_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(graph_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("digraph {\ncomment=\"created by stub solve_fsm\"\n}\n")
        return out


def _support(variables, satisfying):
    """The variables a truth table actually depends on."""
    support = []
    for index, name in enumerate(variables):
        for entry in satisfying:
            flipped = list(entry)
            flipped[index] = 1 - flipped[index]
            if tuple(flipped) not in satisfying:
                support.append(name)
                break
    return support


def _assignments(variables):
    variables = sorted(variables)
    for value in range(1 << len(variables)):
        yield {name: (value >> index) & 1 for index, name in enumerate(variables)}


# ---------------------------------------------------------------------------
# the fixture, gate for gate
# ---------------------------------------------------------------------------


def _library():
    ff = GateType(
        "FF",
        ["sequential", "ff"],
        [
            Pin("C", "input", "clock"),
            Pin("CE", "input", "enable"),
            Pin("D", "input", "data"),
            Pin("Q", "output", "state"),
        ],
    )
    ffr = GateType(
        "FFR",
        ["sequential", "ff"],
        [
            Pin("C", "input", "clock"),
            Pin("CE", "input", "enable"),
            Pin("D", "input", "data"),
            Pin("R", "input", "reset"),
            Pin("Q", "output", "state"),
        ],
    )
    inv = GateType(
        "INV", ["combinational", "c_inverter"], [Pin("I", "input"), Pin("O", "output")]
    )
    buf = GateType(
        "BUF", ["combinational", "c_buffer"], [Pin("I", "input"), Pin("O", "output")]
    )
    mux = GateType(
        "MUX",
        ["combinational", "c_mux"],
        [
            Pin("I0", "input", "data"),
            Pin("I1", "input", "data"),
            Pin("S", "input", "select"),
            Pin("O", "output", "data"),
        ],
    )
    and2 = GateType(
        "AND2",
        ["combinational", "c_and"],
        [Pin("I0", "input"), Pin("I1", "input"), Pin("O", "output")],
    )
    and3 = GateType(
        "AND3",
        ["combinational", "c_and"],
        [Pin("I0", "input"), Pin("I1", "input"), Pin("I2", "input"), Pin("O", "output")],
    )
    xor2 = GateType(
        "XOR",
        ["combinational", "c_xor"],
        [Pin("I0", "input"), Pin("I1", "input"), Pin("O", "output")],
    )
    gnd = GateType("GND", ["combinational", "ground"], [Pin("O", "output", "ground")])
    vcc = GateType("VCC", ["combinational", "power"], [Pin("O", "output", "power")])
    return {
        "FF": ff,
        "FFR": ffr,
        "INV": inv,
        "BUF": buf,
        "MUX": mux,
        "AND2": and2,
        "AND3": and3,
        "XOR": xor2,
        "GND": gnd,
        "VCC": vcc,
    }


def fixture_netlist(input_filename="tests/fixtures/fsm_controller/controller.v"):
    """The stub twin of ``tests/fixtures/fsm_controller/controller.v``."""
    library = _library()
    netlist = Netlist(design_name="ctrl_top", input_filename=input_filename)

    names = [
        "clk",
        "i_go",
        "i_fin",
        "i_tick",
        "i_rst",
        "o_busy",
        "o_done",
        "o_cnt0",
        "o_cap",
        "const0",
        "const1",
        "q_a",
        "q_b",
        "d_a",
        "d_b",
        "n_b",
        "n_fin",
        "m_a",
        "q_c0",
        "q_c1",
        "q_c2",
        "d_c0",
        "d_c1",
        "d_c2",
        "c1en",
        "c2en",
        "q_d0",
        "q_d1",
    ]
    net = {name: netlist.net(name) for name in names}
    # exactly what plugins/verilog_parser stores for #(.INIT(1'h0)): the literal
    # is converted to hex and typed "bit_value" when it is a single 0 or 1
    init0 = {("generic", "INIT"): ("bit_value", "0")}

    netlist.gate("u_gnd", library["GND"], {"O": net["const0"]})
    netlist.gate("u_vcc", library["VCC"], {"O": net["const1"]})

    netlist.gate(
        "nx_a1_reg",
        library["FFR"],
        {"C": net["clk"], "CE": net["const1"], "D": net["d_a"], "R": net["const0"], "Q": net["q_a"]},
        data=init0,
    )
    netlist.gate(
        "nx_b2_reg",
        library["FFR"],
        {"C": net["clk"], "CE": net["const1"], "D": net["d_b"], "R": net["const0"], "Q": net["q_b"]},
        data=init0,
    )
    netlist.gate("u_inv_b", library["INV"], {"I": net["q_b"], "O": net["n_b"]})
    netlist.gate("u_inv_f", library["INV"], {"I": net["i_fin"], "O": net["n_fin"]})
    netlist.gate(
        "u_mux_a",
        library["MUX"],
        {"I0": net["i_go"], "I1": net["n_fin"], "S": net["q_a"], "O": net["m_a"]},
    )
    netlist.gate("u_and_a", library["AND2"], {"I0": net["n_b"], "I1": net["m_a"], "O": net["d_a"]})
    netlist.gate(
        "u_and_b",
        library["AND3"],
        {"I0": net["n_b"], "I1": net["q_a"], "I2": net["i_fin"], "O": net["d_b"]},
    )
    netlist.gate("u_busy_buf", library["BUF"], {"I": net["q_a"], "O": net["o_busy"]})
    netlist.gate("u_done_buf", library["BUF"], {"I": net["q_b"], "O": net["o_done"]})

    netlist.gate(
        "cnt_r0",
        library["FFR"],
        {"C": net["clk"], "CE": net["const1"], "D": net["d_c0"], "R": net["i_rst"], "Q": net["q_c0"]},
        data=init0,
    )
    netlist.gate(
        "cnt_r1",
        library["FFR"],
        {"C": net["clk"], "CE": net["const1"], "D": net["d_c1"], "R": net["i_rst"], "Q": net["q_c1"]},
        data=init0,
    )
    netlist.gate(
        "cnt_r2",
        library["FFR"],
        {"C": net["clk"], "CE": net["const1"], "D": net["d_c2"], "R": net["i_rst"], "Q": net["q_c2"]},
        data=init0,
    )
    netlist.gate("u_x0", library["XOR"], {"I0": net["q_c0"], "I1": net["i_tick"], "O": net["d_c0"]})
    netlist.gate(
        "u_c1en", library["AND2"], {"I0": net["q_c0"], "I1": net["i_tick"], "O": net["c1en"]}
    )
    netlist.gate("u_x1", library["XOR"], {"I0": net["q_c1"], "I1": net["c1en"], "O": net["d_c1"]})
    netlist.gate(
        "u_c2en",
        library["AND3"],
        {"I0": net["q_c0"], "I1": net["q_c1"], "I2": net["i_tick"], "O": net["c2en"]},
    )
    netlist.gate("u_x2", library["XOR"], {"I0": net["q_c2"], "I1": net["c2en"], "O": net["d_c2"]})
    netlist.gate("u_cnt0_buf", library["BUF"], {"I": net["q_c0"], "O": net["o_cnt0"]})

    netlist.gate(
        "dp_x0",
        library["FF"],
        {"C": net["clk"], "CE": net["const1"], "D": net["i_go"], "Q": net["q_d0"]},
        data=init0,
    )
    netlist.gate(
        "dp_x1",
        library["FF"],
        {"C": net["clk"], "CE": net["const1"], "D": net["q_d0"], "Q": net["q_d1"]},
        data=init0,
    )
    netlist.gate("u_cap_buf", library["BUF"], {"I": net["q_d1"], "O": net["o_cap"]})

    return netlist, net


def controller_model(net):
    """``(variables, next_state)`` for the controller, in net-variable terms."""
    go = "net_{}".format(net["i_go"].get_id())
    fin = "net_{}".format(net["i_fin"].get_id())

    def next_state(state, assignment):
        a = state & 1
        b = (state >> 1) & 1
        n_b = 1 - b
        m_a = (1 - assignment[fin]) if a else assignment[go]
        d_a = n_b & m_a
        d_b = n_b & a & assignment[fin]
        return d_a | (d_b << 1)

    return [go, fin], next_state


def counter_model(net):
    """``(variables, next_state)`` for the 3-bit counter."""
    tick = "net_{}".format(net["i_tick"].get_id())

    def next_state(state, assignment):
        return (state + 1) % 8 if assignment[tick] else state

    return [tick], next_state
