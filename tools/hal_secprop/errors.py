"""The failure kinds this analysis distinguishes.

They are separate types because they mean different things to a caller: a
policy that does not parse is the analyst's problem, a design that uses a
primitive the model does not cover is a *coverage* result that still has to be
reported as findings, and a broken engine is neither.
"""

__all__ = [
    "SecpropError",
    "PolicyError",
    "DesignError",
    "UnsupportedPrimitives",
    "EngineError",
]


class SecpropError(RuntimeError):
    """Base class for every deliberate failure of this tool."""


class PolicyError(SecpropError):
    """The policy document is malformed, or does not fit the design."""


class DesignError(SecpropError):
    """The design could not be read or turned into a transition system."""


class UnsupportedPrimitives(DesignError):
    """The design uses primitives this analysis does not model.

    This is not an internal failure: it is a coverage statement, and the CLI
    turns it into ``unsupported`` findings rather than an error exit, because
    "we did not model this" and "the analysis crashed" must never look the same
    in a report.

    ``primitives`` is a list of
    ``{"gate_type", "reason", "count", "example_gates"}`` dicts -- the same
    shape :class:`hal_apb_check.netlist.UnsupportedPrimitives` uses, so the
    offline and the ``hal_py`` front ends report identically.
    """

    def __init__(self, message, primitives):
        self.primitives = [dict(entry) for entry in primitives]
        DesignError.__init__(self, message)


class EngineError(SecpropError):
    """The check cannot start: the policy and the design do not fit together."""
