"""Reference model of ``lut_logic.v`` -- the ground truth for this fixture.

Six inputs, so the comparison against the exported netlist is exhaustive: all
64 input assignments are evaluated.
"""

KIND = "combinational"

INPUTS = [(name, 1) for name in ("a", "b", "c", "d", "e", "f")]
OUTPUTS = [("y0", 1), ("y1", 1), ("y2", 1)]


def evaluate(values):
    a, b, c, d, e, f = (values[name] for name in ("a", "b", "c", "d", "e", "f"))
    return {
        "y0": ((a & b) | ((1 - c) & d) | (e ^ f)) & 1,
        "y1": ((a ^ b ^ c) & (d | (1 - e))) & 1,
        "y2": ((a & (1 - b) & c) | (d & e & (1 - f)) | ((1 - a) & f)) & 1,
    }
