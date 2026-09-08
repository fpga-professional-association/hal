"""Inserting the injection hardware into a netlist, once, before anything runs.

For every fault site the netlist is rewired from::

    FF --Q--> N --> (whatever N drove)

to::

    FF --Q--> N_raw --> XOR --> N --> (whatever N drove, unchanged)
                         ^
                    ctrl (new global input, one per site)

Three properties matter and each is checked rather than assumed:

* **N keeps its identity.**  The original net object -- with its name, its ID,
  its destinations and its global-output flag -- is the one the XOR drives.
  Nothing downstream, including the observation of primary outputs, has to know
  that anything changed.
* **The instrumentation is the identity at ctrl=0.**  ``XOR(x, 0) = x``, so the
  fault-free baseline of the instrumented netlist is the behaviour of the
  original netlist.  The campaign verifies that empirically as well, by
  simulating the uninstrumented netlist and comparing traces.
* **Only what can be instrumented is.**  A sequential gate with no ``state``
  output pin, or whose state pin drives nothing, cannot host the model; it is
  returned in ``skipped`` with a reason and ends up in an ``unsupported``
  finding, never silently dropped.

The XOR gate type is looked up in the netlist's *own* gate library.  A library
without a two-input XOR cannot be instrumented, and that is reported as a
configuration error rather than worked around with a LUT the library may or may
not have.
"""

from ..campaign import Site, control_net_name, injector_gate_name, raw_net_name

__all__ = ["InstrumentationError", "XOR_CANDIDATES", "find_sites", "instrument"]


class InstrumentationError(RuntimeError):
    """Raised when the netlist cannot host the fault model."""


#: Two-input XOR cells, by the names the shipped libraries use.  Checked in
#: order; the first one the library defines is used and recorded in the manifest.
XOR_CANDIDATES = ("XOR", "XOR2", "XOR2X1", "XOR2_X1", "XNOR2")


def _state_pins(hal_py, gate):
    """Output pins of ``gate`` that carry the stored state."""
    pins = []
    for pin in gate.get_type().get_pins():
        try:
            is_output = pin.get_direction() == hal_py.PinDirection.output
        except AttributeError:  # pragma: no cover - older bindings
            is_output = True
        if is_output and pin.get_type() == hal_py.PinType.state:
            pins.append(pin)
    return pins


def find_sites(hal_py, netlist):
    """Every instrumentable register output, plus the ones that are not.

    Returns ``(sites, skipped)``.  ``sites`` are :class:`Site` objects in no
    particular order -- :func:`hal_fault_campaign.campaign.sort_sites` decides
    the canonical one.
    """
    sites = []
    skipped = []
    for gate in netlist.get_gates():
        gate_type = gate.get_type()
        if not gate_type.has_property(hal_py.GateTypeProperty.sequential):
            continue
        type_name = gate_type.get_name()
        pins = _state_pins(hal_py, gate)
        if not pins:
            skipped.append(
                {
                    "gate_name": gate.get_name(),
                    "gate_id": gate.get_id(),
                    "gate_type": type_name,
                    "reason": (
                        "the gate type has no output pin of type 'state', so there is no "
                        "single net carrying the stored bit to invert"
                    ),
                }
            )
            continue
        attached = False
        for pin in pins:
            net = gate.get_fan_out_net(pin)
            if net is None:
                continue
            attached = True
            sites.append(
                Site(
                    gate.get_name(),
                    gate.get_id(),
                    type_name,
                    pin.get_name(),
                    net.get_name(),
                    net.get_id(),
                )
            )
        if not attached:
            skipped.append(
                {
                    "gate_name": gate.get_name(),
                    "gate_id": gate.get_id(),
                    "gate_type": type_name,
                    "reason": "the gate's state output pin drives no net",
                }
            )
    return sites, skipped


def _xor_type(netlist):
    library = netlist.get_gate_library()
    for name in XOR_CANDIDATES:
        gate_type = library.get_gate_type_by_name(name)
        if gate_type is not None:
            return name, gate_type
    raise InstrumentationError(
        "the gate library {!r} defines none of {}; the injection model needs a two-input "
        "XOR cell to insert on the register output".format(
            library.get_name(), ", ".join(XOR_CANDIDATES)
        )
    )


def _xor_pins(gate_type):
    inputs = [pin.get_name() for pin in gate_type.get_input_pins()]
    outputs = [pin.get_name() for pin in gate_type.get_output_pins()]
    if len(inputs) != 2 or len(outputs) != 1:
        raise InstrumentationError(
            "gate type {!r} has {} input and {} output pin(s); the injector needs exactly "
            "two inputs and one output".format(
                gate_type.get_name(), len(inputs), len(outputs)
            )
        )
    return inputs[0], inputs[1], outputs[0]


def instrument(hal_py, netlist, sites):
    """Rewire ``sites`` for injection. Returns the instrumentation record.

    Mutates ``netlist`` in place and updates each :class:`Site` with the control
    net that drives its injector.
    """
    type_name, xor_type = _xor_type(netlist)
    pin_a, pin_b, pin_out = _xor_pins(xor_type)

    if type_name == "XNOR2":  # pragma: no cover - no shipped library needs it
        raise InstrumentationError(
            "only a true XOR can be transparent at ctrl=0; {!r} is an XNOR and would "
            "invert the baseline".format(type_name)
        )

    record = {
        "injector_gate_type": type_name,
        "injector_pins": {"data": pin_a, "control": pin_b, "output": pin_out},
        "sites": [],
    }

    for site in sites:
        gate = netlist.get_gate_by_id(site.gate_id)
        if gate is None:
            raise InstrumentationError(
                "gate ID {} ({!r}) vanished between site discovery and "
                "instrumentation".format(site.gate_id, site.gate_name)
            )
        original = netlist.get_net_by_id(site.net_id)
        if original is None:
            raise InstrumentationError(
                "net ID {} ({!r}) vanished between site discovery and "
                "instrumentation".format(site.net_id, site.net_name)
            )

        raw = netlist.create_net(raw_net_name(site.gate_id, site.output_pin))
        control = netlist.create_net(control_net_name(site.gate_id))
        injector = netlist.create_gate(
            xor_type, injector_gate_name(site.gate_id, site.output_pin)
        )
        if raw is None or control is None or injector is None:
            raise InstrumentationError(
                "could not create the injector for {!r}; a name collision with an "
                "existing net or gate is the likely cause".format(site.gate_name)
            )

        if not original.remove_source(gate, site.output_pin):
            raise InstrumentationError(
                "could not detach {!r}.{} from net {!r}".format(
                    site.gate_name, site.output_pin, site.net_name
                )
            )
        if raw.add_source(gate, site.output_pin) is None:
            raise InstrumentationError(
                "could not attach {!r}.{} to its new raw net".format(
                    site.gate_name, site.output_pin
                )
            )
        if raw.add_destination(injector, pin_a) is None:
            raise InstrumentationError("could not wire the raw net into the injector")
        if control.add_destination(injector, pin_b) is None:
            raise InstrumentationError("could not wire the control net into the injector")
        if original.add_source(injector, pin_out) is None:
            raise InstrumentationError(
                "could not make the injector drive {!r}".format(site.net_name)
            )
        netlist.mark_global_input_net(control)

        site.control_net = control.get_name()
        record["sites"].append(
            {
                "gate_name": site.gate_name,
                "gate_id": site.gate_id,
                "output_pin": site.output_pin,
                "original_net": site.net_name,
                "original_net_id": site.net_id,
                "raw_net": raw.get_name(),
                "control_net": site.control_net,
                "injector_gate": injector.get_name(),
            }
        )

    return record
