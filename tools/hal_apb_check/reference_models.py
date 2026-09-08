"""Hand-written transition systems mirroring the shipped Verilog fixtures.

Each model is a gate-for-gate transcription of the corresponding netlist in
``fixtures/``: same signal names, same Boolean functions, same flip-flop
semantics (``EXAMPLE_GATE_LIBRARY``'s ``FFR`` is ``Q' = R ? 0 : (D & CE)``, and
every fixture ties ``CE`` high).

They exist for two reasons. First, the whole property engine -- unrolling,
assumptions, vacuity, counterexample replay -- can then be tested on a plain
Python interpreter, with no HAL build in sight. Second, and more usefully, the
container test loads the *Verilog* through ``hal_py``, extracts a transition
system from the netlist, and proves it equivalent to the model here. That turns
a hand-written stand-in from a liability into a cross-check: if the netlist front
end mis-reads a gate, or the fixture is edited without updating the model, the
equivalence miter fails and says which signal disagrees.

Expected verdicts for every model are stated in ``fixtures/README.md``.
"""

from .expr import and_, not_, or_, var, xor_
from .system import TransitionSystem, ff_next

__all__ = ["MODELS", "build", "names"]


def _completer(name, ready_stalled=False, slverr_during_wait=False):
    """The shared skeleton of the three completer fixtures.

    One wait state: ``w_q`` toggles while the ACCESS phase is held, so ``PREADY``
    goes high in the second ACCESS cycle.
    """
    system = TransitionSystem(name)
    system.add_input("PRESETn")
    system.add_input("PSEL")
    system.add_input("PENABLE")
    system.add_input("PWRITE")
    system.add_input("PADDR_0")
    system.add_input("PWDATA_0")
    system.add_input("ERR_IN")
    if ready_stalled:
        system.add_input("STALL_N")
    # No initial-state assumption: the reset sequence has to establish it.
    system.add_state("w_q", initial=None)

    system.define("rst", not_(var("PRESETn")))
    system.define("access", and_(var("PSEL"), var("PENABLE")))
    system.define("w_n", not_(var("w_q")))
    system.define("w_d", and_(var("access"), var("w_n")))
    if ready_stalled:
        system.define("PREADY", and_(var("access"), var("w_q"), var("STALL_N")))
    else:
        system.define("PREADY", and_(var("access"), var("w_q")))
    if slverr_during_wait:
        system.define("nready", not_(var("PREADY")))
        system.define("PSLVERR", and_(var("access"), var("nready"), var("ERR_IN")))
    else:
        system.define("PSLVERR", and_(var("PREADY"), var("ERR_IN")))
    system.define("dat", and_(var("PWRITE"), var("PADDR_0"), var("PWDATA_0")))
    system.define("PRDATA_0", xor_(var("w_q"), var("dat")))

    system.set_next("w_q", ff_next(var("w_d"), clear=var("rst")))
    return system


def completer_ok():
    """Correct APB4 completer: one wait state, PSLVERR only with PREADY."""
    return _completer("apb_completer_ok")


def completer_broken_slverr():
    """Completer that raises PSLVERR during a wait state, before the transfer completes."""
    return _completer("apb_completer_broken_slverr", slverr_during_wait=True)


def completer_broken_stall():
    """Completer whose PREADY can be held off indefinitely by STALL_N."""
    return _completer("apb_completer_broken_stall", ready_stalled=True)


def _requester(name, hold_address=True):
    """The shared skeleton of the two requester fixtures."""
    system = TransitionSystem(name)
    system.add_input("PRESETn")
    system.add_input("START")
    system.add_input("ADDR_IN")
    system.add_input("WR_IN")
    system.add_input("WDATA_IN")
    system.add_input("PREADY")
    for register in ("sel_q", "en_q", "addr_q", "wr_q", "wdat_q"):
        system.add_state(register, initial=None)

    system.define("rst", not_(var("PRESETn")))
    system.define("PSEL", var("sel_q"))
    system.define("PENABLE", var("en_q"))
    system.define("PADDR_0", var("addr_q"))
    system.define("PWRITE", var("wr_q"))
    system.define("PWDATA_0", var("wdat_q"))

    system.define("done", and_(var("en_q"), var("PREADY")))
    system.define("ndone", not_(var("done")))
    system.define("sel_hold", and_(var("sel_q"), var("ndone")))
    system.define("nsel", not_(var("sel_q")))
    system.define("start_sel", and_(var("nsel"), var("START")))
    system.define("sel_d", or_(var("sel_hold"), var("start_sel")))

    system.define("nen", not_(var("en_q")))
    system.define("setup", and_(var("sel_q"), var("nen")))
    system.define("npready", not_(var("PREADY")))
    system.define("waiting", and_(var("en_q"), var("npready")))
    system.define("en_d", or_(var("setup"), var("waiting")))

    def mux(low, high):
        # EXAMPLE_GATE_LIBRARY MUX: O = (!S & I0) | (S & I1)
        return or_(and_(var("nsel"), low), and_(var("sel_q"), high))

    if hold_address:
        system.define("addr_d", mux(var("ADDR_IN"), var("addr_q")))
    else:
        system.define("addr_d", var("ADDR_IN"))
    system.define("wr_d", mux(var("WR_IN"), var("wr_q")))
    system.define("wdat_d", mux(var("WDATA_IN"), var("wdat_q")))

    clear = var("rst")
    system.set_next("sel_q", ff_next(var("sel_d"), clear=clear))
    system.set_next("en_q", ff_next(var("en_d"), clear=clear))
    system.set_next("addr_q", ff_next(var("addr_d"), clear=clear))
    system.set_next("wr_q", ff_next(var("wr_d"), clear=clear))
    system.set_next("wdat_q", ff_next(var("wdat_d"), clear=clear))
    return system


def requester_ok():
    """Correct APB4 requester: control held from SETUP through every wait state."""
    return _requester("apb_requester_ok")


def requester_broken_stability():
    """Requester whose PADDR follows its input register bypass, breaking stability."""
    return _requester("apb_requester_broken_stability", hold_address=False)


MODELS = {
    "completer_ok": completer_ok,
    "completer_broken_slverr": completer_broken_slverr,
    "completer_broken_stall": completer_broken_stall,
    "requester_ok": requester_ok,
    "requester_broken_stability": requester_broken_stability,
}

def names():
    return sorted(MODELS)


def build(name):
    """Instantiate a reference model by name."""
    factory = MODELS.get(name)
    if factory is None:
        raise KeyError(
            "unknown reference model {!r}; available models are {}".format(
                name, ", ".join(names())
            )
        )
    return factory()
