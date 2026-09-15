"""Executable model of `design.v` / `spec.md`.

Pure Python, standard library only.  `check.py` drives this side by side with
the *exported netlist* (simulated by `tools/hal_agilex`) and requires identical
outputs, which is what makes the walkthrough's claims about the netlist
checkable rather than asserted.

Naming and semantics follow `design.v` exactly, including the constant top bit
of the shift register (`shreg[9]`), which the model keeps and synthesis deletes.

Two interfaces onto the same model, so there is only ever one model:

*   `UartTx`, a mutable object, which is what `check.py` and `probe.py` drive;
*   the `KIND` / `INPUTS` / `initial_state` / `outputs` / `next_state` surface
    at the bottom of this file, which is the contract `tools/hal_agilex`
    consumes -- it makes `hal_agilex behavior` and `hal_agilex trace` usable on
    this walkthrough without a second, separately maintained restatement of
    the RTL that could quietly drift from the first.
"""

DIV_W = 4
N_SLOTS = 10
CLK_DIV = 1 << DIV_W

DIV_MASK = (1 << DIV_W) - 1
BIT_MASK = 0xF
SHREG_MASK = (1 << N_SLOTS) - 1


class UartTx(object):
    """One clock domain, positive edge, asynchronous active-low clear."""

    def __init__(self):
        self.clear()

    def clear(self):
        """What `rst_n = 0` does, asynchronously."""
        self.baud_cnt = 0
        self.bit_cnt = 0
        self.shreg = SHREG_MASK   # idle line is high
        self.busy = 0

    # -- outputs (combinational) ------------------------------------------

    @property
    def tx(self):
        return self.shreg & 1

    @property
    def tx_busy(self):
        return self.busy

    # -- one rising clock edge --------------------------------------------

    def clock(self, tx_start=0, tx_data=0, rst_n=1):
        if not rst_n:
            self.clear()
            return

        if not self.busy:
            self.baud_cnt = 0
            self.bit_cnt = 0
            if tx_start:
                # {stop, data LSB-first, start}
                self.shreg = ((1 << (N_SLOTS - 1)) | ((tx_data & 0xFF) << 1)) & SHREG_MASK
                self.busy = 1
            return

        baud_tick = self.baud_cnt == DIV_MASK
        frame_end = self.bit_cnt == N_SLOTS

        self.baud_cnt = (self.baud_cnt + 1) & DIV_MASK
        if baud_tick:
            self.shreg = ((self.shreg >> 1) | (1 << (N_SLOTS - 1))) & SHREG_MASK
            self.bit_cnt = (self.bit_cnt + 1) & BIT_MASK
        if frame_end:
            self.busy = 0


def frame_bits(byte):
    """The ten slot values the spec says a frame carries, in transmit order."""
    return [0] + [(byte >> i) & 1 for i in range(8)] + [1]


def transmit(byte, settle=4):
    """Drive one frame and return the per-clock `(tx, tx_busy)` trace.

    The trace starts with the load edge, so `trace[0]` is the first cycle of the
    start bit.  Runs `settle` extra cycles past the end of the frame.
    """
    dut = UartTx()
    dut.clear()
    trace = []
    dut.clock(tx_start=1, tx_data=byte)          # the load edge
    while dut.tx_busy:
        trace.append((dut.tx, dut.tx_busy))
        dut.clock()
    for _ in range(settle):
        trace.append((dut.tx, dut.tx_busy))
        dut.clock()
    return trace


def sample_frame(byte):
    """Sample the middle of every slot of one frame -- what a receiver sees."""
    trace = transmit(byte)
    return [trace[slot * CLK_DIV + CLK_DIV // 2][0] for slot in range(N_SLOTS)]


# ---------------------------------------------------------------------------
# the tools/hal_agilex reference contract
#
# `UartTx` above is the model; everything below is the plain-data view of it
# that `hal_agilex behavior` and `hal_agilex trace` expect.  State is a dict
# because those drivers treat it as an opaque immutable value they may hold on
# to, which a mutable object would not survive.
# ---------------------------------------------------------------------------

KIND = "sequential"

#: Exactly the ports `uart_tx.vo` declares.
INPUTS = [("clk", 1), ("rst_n", 1), ("tx_start", 1), ("tx_data", 8)]
OUTPUTS = [("tx", 1), ("tx_busy", 1)]

#: The export has no clock net to drive; the simulator steps the registers.
IGNORED_INPUTS = ["clk"]
#: Active-low asynchronous clear, on every flip-flop's `clrn`.
ASYNC_CLEAR_INPUT = "rst_n"

_STATE_FIELDS = ("baud_cnt", "bit_cnt", "shreg", "busy")


def _snapshot(dut):
    return {field: getattr(dut, field) for field in _STATE_FIELDS}


def _restore(state):
    dut = UartTx()
    for field in _STATE_FIELDS:
        setattr(dut, field, state[field])
    return dut


def initial_state():
    """What `rst_n = 0` leaves behind: idle line high, nothing in flight."""
    return _snapshot(UartTx())


def outputs(state, values):
    dut = _restore(state)
    return {"tx": dut.tx, "tx_busy": dut.tx_busy}


def next_state(state, values):
    dut = _restore(state)
    dut.clock(
        tx_start=values["tx_start"],
        tx_data=values["tx_data"],
        rst_n=values["rst_n"],
    )
    return _snapshot(dut)
