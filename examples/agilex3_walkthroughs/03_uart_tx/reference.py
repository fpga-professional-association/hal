"""Executable model of `design.v` / `spec.md`.

Pure Python, standard library only.  `check.py` drives this side by side with
the *exported netlist* (simulated by `tools/hal_agilex`) and requires identical
outputs, which is what makes the walkthrough's claims about the netlist
checkable rather than asserted.

Naming and semantics follow `design.v` exactly, including the constant top bit
of the shift register (`shreg[9]`), which the model keeps and synthesis deletes.
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
