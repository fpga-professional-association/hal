"""Python model of ``ground_truth/designs/core_b.v`` -- the decoy's RTL.

Used only in the reveal: ``tools/hal_agilex behavior`` simulates the *named*
export against this model and requires agreement for every output in every
cycle, which is what turns "core B is a link framer" from an assertion into a
checked statement.  Two deliberately wrong copies of it are run alongside (see
``run_analysis.sh``) so that the agreement means something.

Nothing in the blind half of the walkthrough reads this file.
"""

KIND = "sequential"

INPUTS = [
    ("clk", 1),
    ("rst_n", 1),
    ("rx", 1),
]

OUTPUTS = [
    ("rx_data", 8),
    ("rx_valid", 1),
    ("frame_ok", 1),
    ("frame_err", 1),
    ("link_state", 4),
]

#: The netlist has no clock net for the simulator to drive: it steps registers.
IGNORED_INPUTS = ["clk"]
#: Active-low asynchronous reset.
ASYNC_CLEAR_INPUT = "rst_n"

#: The start-of-frame and end-of-frame byte.
DELIMITER = 0x7E
#: The watchdog fires when the in-frame counter reaches this value.  A frame is
#: at most 255 payload bytes, so it cannot last 65536 cycles and the watchdog is
#: **unreachable** under any stimulus that keeps delivering bits.  That is a
#: property of the RTL, it is deliberate, and the reveal's negative controls use
#: it: a control that moves this constant passes a clean behaviour run of any
#: length, which is what a coverage hole looks like from inside a bounded check.
AGE_LIMIT = 0xFFFF

HUNT, LEN, DATA, TAIL = 0, 1, 2, 3


def initial_state():
    return {
        "sh": 0,
        "bit_cnt": 0,
        "len_cnt": 0,
        "age": 0,
        "data": 0,
        "valid": 0,
        "ok": 0,
        "err": 0,
        "state": 1 << HUNT,
    }


def _bit(word, index):
    return (word >> index) & 1


def outputs(state, values):
    return {
        "rx_data": state["data"],
        "rx_valid": state["valid"],
        "frame_ok": state["ok"],
        "frame_err": state["err"],
        "link_state": state["state"],
    }


def next_state(state, values):
    # The clear is asynchronous and dominates, exactly as the clrn pin does.
    if not values["rst_n"]:
        return initial_state()

    current = state["state"]
    sh_next = ((state["sh"] << 1) | (values["rx"] & 1)) & 0xFF

    byte_done = 1 if state["bit_cnt"] == 7 else 0
    sync_here = 1 if sh_next == DELIMITER else 0
    len_zero = 1 if sh_next == 0 else 0
    len_last = 1 if state["len_cnt"] == 1 else 0
    expired = 1 if state["age"] == AGE_LIMIT else 0

    take_len = _bit(current, LEN) & byte_done
    take_byte = _bit(current, DATA) & byte_done
    close_tail = _bit(current, TAIL) & byte_done

    leave_byte = byte_done | expired
    leave_data = (take_byte & len_last) | expired
    to_tail = take_byte & len_last & (1 - expired)
    to_hunt = close_tail | (take_len & len_zero) | expired

    if take_len:
        len_cnt = sh_next
    elif take_byte:
        len_cnt = (state["len_cnt"] - 1) & 0xFF
    else:
        len_cnt = state["len_cnt"]

    next_bits = 0
    next_bits |= ((_bit(current, HUNT) & (1 - sync_here)) | to_hunt) << HUNT
    next_bits |= (
        (_bit(current, LEN) & (1 - leave_byte)) | (_bit(current, HUNT) & sync_here)
    ) << LEN
    next_bits |= (
        (_bit(current, DATA) & (1 - leave_data)) | (take_len & (1 - len_zero))
    ) << DATA
    next_bits |= ((_bit(current, TAIL) & (1 - leave_byte)) | to_tail) << TAIL

    if _bit(current, HUNT) or expired:
        age = 0
    else:
        age = (state["age"] + 1) & AGE_LIMIT

    return {
        "sh": sh_next,
        "bit_cnt": 0 if _bit(current, HUNT) else (state["bit_cnt"] + 1) & 0x7,
        "len_cnt": len_cnt,
        "age": age,
        "data": sh_next if take_byte else state["data"],
        "valid": take_byte,
        "ok": close_tail & sync_here & (1 - expired),
        "err": (close_tail & (1 - sync_here)) | (take_len & len_zero) | expired,
        "state": next_bits,
    }
