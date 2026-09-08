"""The fault model, written down once so nothing can quietly drift from it.

There is exactly one model in this MVP and it is deliberately narrow:

    ``register_output_transient_flip``
        For a chosen sequential gate *R* and a chosen cycle *c*, the value that
        the rest of the design sees on *R*'s state output is inverted for the
        whole of cycle *c* (or for ``hold_cycles`` consecutive cycles starting
        at *c*).  Everything else is untouched.

How it is realised
------------------
Not by a simulator hook -- there is none.  ``NetlistSimulatorController`` can
set *input* nets between simulation steps and nothing else, so a fault model
that pokes an internal net would have to live inside an engine and would then
work with that engine only.  Instead the netlist itself is instrumented once,
before any simulation runs (:mod:`hal_fault_campaign.steps.instrument`):

    R.Q ---> (new net) ---> XOR ---> the net R.Q used to drive
                             ^
                             |
                    (new global input net, one per site)

With the control input held at 0 the XOR is the identity, so **the instrumented
netlist and the original netlist simulate identically**; the fault-free
baseline is produced from the *same* instrumented netlist as every faulty run,
which is what makes a divergence attributable to the injection rather than to a
netlist edit.  Driving one control input to 1 for one cycle flips exactly one
register's observed output for exactly that cycle.

What that is, and what it is not
--------------------------------
Within a cycle in which the control is asserted, the design behaves exactly as
if that register held the complemented bit: combinational logic downstream sees
the flipped value, and every register that captures at the clock edge inside
the window -- including *R* itself through its own next-state logic -- latches a
value computed from it.  For a register with feedback (a counter, an FSM) the
corruption therefore persists in the state after the window closes, which is
what a real upset does.

For a register whose next state does not depend on its own output (a pipeline
stage, a shift register), the model differs from a physical SEU in one specific
way: a physical upset corrupts the *stored* bit and it stays corrupted until the
register is next written, whereas this model restores the register's own output
when the window closes.  The corrupted value has by then already been captured
downstream, so the propagation is the same; the difference is confined to the
injected register's own output after the window.  ``hold_cycles`` widens the
window when that difference matters.

Other deliberate limitations, all reported with every result:

* zero-delay digital model -- no timing, no glitch propagation, no pulse-width
  filtering, no charge sharing, no multi-bit upsets, no clock/reset-tree faults;
* one fault per simulation run;
* only gates with a ``state`` output pin can be sites; latches, RAM and any
  sequential primitive without one are reported as an explicit coverage gap
  rather than silently skipped;
* a campaign samples the (site x cycle) space and says nothing about a physical
  failure rate.
"""

__all__ = [
    "MODEL_ID",
    "MODELS",
    "MODEL_DESCRIPTIONS",
    "ASSUMPTIONS",
    "LIMITATIONS",
    "CLASS_DETECTED",
    "CLASS_SILENT",
    "CLASS_UNOBSERVED",
    "CLASS_INDETERMINATE",
    "CLASSIFICATIONS",
    "CLASSIFICATION_DESCRIPTIONS",
]

MODEL_ID = "register_output_transient_flip"

MODELS = (MODEL_ID,)

MODEL_DESCRIPTIONS = {
    MODEL_ID: (
        "Single-bit transient: the state output of one sequential gate is inverted for "
        "hold_cycles consecutive clock cycles starting at the injection cycle, realised "
        "by an XOR inserted on the register's output net and driven by a dedicated "
        "control input that is 0 everywhere else."
    ),
}

#: Assumptions every finding this tool emits has to carry, verbatim.
ASSUMPTIONS = (
    (
        "zero-delay-digital-model",
        "The result is a property of HAL's zero-delay digital simulation of the pinned "
        "netlist, not of silicon. No timing, glitch filtering, metastability, "
        "charge-sharing or multi-bit effect is modelled, and no physical upset rate is "
        "implied.",
        "tool",
    ),
    (
        "fault-model-output-transient",
        "A fault is modelled as the inversion of one register's state output for a whole "
        "clock cycle (see hal_fault_campaign.faultmodel). The corrupted value is captured "
        "by everything that clocks inside the window, including the register itself "
        "through its own next-state logic; a register whose next state does not depend on "
        "its own output recovers its output when the window closes, unlike a physical "
        "upset, which holds until the register is next written.",
        "tool",
    ),
    (
        "instrumentation-transparent-at-zero",
        "Baseline and faulty runs use the same instrumented netlist; with every injection "
        "control input at 0 the inserted XOR gates are the identity, so any divergence is "
        "caused by the injection and not by the instrumentation. The campaign checks this "
        "by comparing the instrumented baseline against the uninstrumented netlist.",
        "structural",
    ),
    (
        "single-workload-single-fault",
        "One workload, one fault per simulation run. The claim covers the recorded "
        "stimulus only; a different workload can move a fault between classes.",
        "environment",
    ),
)

#: Limitations reported on the campaign summary finding.
LIMITATIONS = (
    "Unobserved means nothing reached the observed outputs or the detection signals "
    "inside the observation window. It is not masking: the same fault can become visible "
    "later, under a different workload, or on a signal that is not observed here.",
    "Detection latency is measured in whole clock cycles of the simulated workload, from "
    "the injection cycle to the first cycle in which a detection signal is active. It is "
    "not a silicon latency.",
    "A sampled campaign covers a subset of the (register x cycle) space. Class counts are "
    "counts over that subset and must not be read as a failure rate, a diagnostic "
    "coverage figure or a reliability metric.",
    "Only single-bit transients in registers are covered. Faults in combinational logic, "
    "clock and reset trees, memories, I/O and the detection logic itself are out of scope.",
)

CLASS_DETECTED = "detected"
CLASS_SILENT = "silent_divergence"
CLASS_UNOBSERVED = "unobserved_in_window"
CLASS_INDETERMINATE = "indeterminate"

CLASSIFICATIONS = (CLASS_DETECTED, CLASS_SILENT, CLASS_UNOBSERVED, CLASS_INDETERMINATE)

CLASSIFICATION_DESCRIPTIONS = {
    CLASS_DETECTED: (
        "A detection signal became active inside the observation window while the "
        "fault-free baseline left it inactive."
    ),
    CLASS_SILENT: (
        "An observed output differs from the fault-free baseline inside the observation "
        "window and no detection signal ever became active: externally visible, "
        "undetected corruption."
    ),
    CLASS_UNOBSERVED: (
        "Neither an observed output nor a detection signal differed from the fault-free "
        "baseline inside the observation window. This is a statement about the window, "
        "not a masking claim."
    ),
    CLASS_INDETERMINATE: (
        "The faulty run produced an undefined (X) or high-impedance (Z) value where the "
        "baseline was defined, so whether the output diverges cannot be decided from the "
        "simulation."
    ),
}
