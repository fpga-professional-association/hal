"""The APB property catalogue: one revision, one role, obligations vs assumptions.

Every property in this file states three things that a report must never blur:

* **which revision** of AMBA APB it comes from (``APB2``/``APB3``/``APB4``) --
  ``PREADY`` and ``PSLVERR`` do not exist before APB3, ``PSTRB``/``PPROT`` do not
  exist before APB4, so a check that mentions them is *unsupported* on an older
  revision rather than trivially true;
* **whose obligation** it is -- a requester (master) property is something the
  DUT must satisfy when the DUT is the requester, and something the checker
  *assumes* about the environment when the DUT is the completer. Getting this
  backwards is the classic way to "prove" a design correct by constraining the
  bug away, so the direction is data here, not a comment;
* **how far into the future** it looks (``horizon``), because a bounded run can
  only instantiate a property at cycles ``t`` with ``t + horizon <= bound`` and
  the resulting coverage gap has to be reported.

Deliberately *not* covered -- see :data:`EXCLUSIONS` and the module README:
APB5 (``PWAKEUP``, parity, RME, user signalling), AXI4-Lite, automatic bus
discovery, multi-completer address decoding, ``PRDATA``/``PSTRB``/``PPROT``
*value* correctness, and anything sub-cycle (clock trees, gated clocks,
setup/hold, X-propagation).
"""

from . import expr

__all__ = [
    "REVISIONS",
    "ROLES",
    "REQUESTER_DRIVEN",
    "COMPLETER_DRIVEN",
    "SIGNALS",
    "BUS_SIGNALS",
    "Property",
    "PROPERTIES",
    "EXCLUSIONS",
    "revision_index",
    "signals_for_revision",
    "properties_for",
    "obligations_for",
    "assumptions_for",
]

#: Supported APB revisions, oldest first.
REVISIONS = ("APB2", "APB3", "APB4")

#: The role the design under test plays on the bus.
ROLES = ("requester", "completer")

#: Signal -> revision it was introduced in.
SIGNALS = {
    "PCLK": "APB2",
    "PRESETn": "APB2",
    "PADDR": "APB2",
    "PSEL": "APB2",
    "PENABLE": "APB2",
    "PWRITE": "APB2",
    "PWDATA": "APB2",
    "PRDATA": "APB2",
    "PREADY": "APB3",
    "PSLVERR": "APB3",
    "PPROT": "APB4",
    "PSTRB": "APB4",
}

#: Signals driven by the requester (and therefore assumed when the DUT is a completer).
REQUESTER_DRIVEN = ("PADDR", "PSEL", "PENABLE", "PWRITE", "PWDATA", "PPROT", "PSTRB")
#: Signals driven by the completer.
COMPLETER_DRIVEN = ("PREADY", "PRDATA", "PSLVERR")
#: Signals that are vectors rather than single bits.
BUS_SIGNALS = ("PADDR", "PWDATA", "PRDATA", "PSTRB", "PPROT")

#: Requester control signals that must not change while a transfer is in flight.
#: ``PWDATA`` is conditional -- it only carries meaning on a write.
STABLE_CONTROL = ("PADDR", "PWRITE", "PPROT", "PSTRB")


def revision_index(revision):
    try:
        return REVISIONS.index(revision)
    except ValueError:
        raise ValueError(
            "unknown APB revision {!r}; this checker covers {}".format(
                revision, ", ".join(REVISIONS)
            )
        )


def signals_for_revision(revision):
    """Signals that exist in ``revision``."""
    limit = revision_index(revision)
    return tuple(
        name for name, introduced in sorted(SIGNALS.items())
        if revision_index(introduced) <= limit
    )


class Property(object):
    """One temporal obligation, instantiated per cycle as ``antecedent -> consequent``."""

    def __init__(
        self,
        property_id,
        title,
        obligation_of,
        min_revision,
        kind,
        horizon,
        requires,
        antecedent,
        consequent,
        description,
        severity="high",
        optional=False,
        max_revision=None,
        assumable=True,
    ):
        if obligation_of not in ROLES:
            raise ValueError("obligation_of must be one of {}".format(ROLES))
        self.id = property_id
        self.title = title
        self.obligation_of = obligation_of
        self.min_revision = min_revision
        self.max_revision = max_revision
        self.kind = kind
        self.horizon = horizon
        self.requires = tuple(requires)
        self._antecedent = antecedent
        self._consequent = consequent
        self.description = description
        self.severity = severity
        self.optional = optional
        #: May this property be *assumed* of the environment on the other side?
        #: ``False`` for house rules and for bounded budgets we invented: assuming
        #: "the completer is ready within N cycles" while checking a requester
        #: would erase exactly the long wait states a stability bug hides in.
        self.assumable = assumable

    def applies_to_revision(self, revision):
        index = revision_index(revision)
        if index < revision_index(self.min_revision):
            return False
        if self.max_revision is not None and index > revision_index(self.max_revision):
            return False
        return True

    def horizon_for(self, context):
        """Cycles into the future this property reads (may depend on options)."""
        if callable(self.horizon):
            return self.horizon(context)
        return self.horizon

    def antecedent(self, context, cycle):
        return self._antecedent(context, cycle)

    def consequent(self, context, cycle):
        return self._consequent(context, cycle)

    def __repr__(self):
        return "<Property {}>".format(self.id)


# ---------------------------------------------------------------------------
# helpers shared by the property bodies
# ---------------------------------------------------------------------------


def _running(context, cycle, span=0):
    """Reset is inactive for the whole window ``[cycle, cycle + span]``."""
    return expr.and_(
        *[expr.not_(context.reset_active(cycle + offset)) for offset in range(span + 1)]
    )


def _setup_phase(context, cycle):
    return expr.and_(context.bit("PSEL", cycle), expr.not_(context.bit("PENABLE", cycle)))


def _access_phase(context, cycle):
    return expr.and_(context.bit("PSEL", cycle), context.bit("PENABLE", cycle))


def _wait_state(context, cycle):
    return expr.and_(_access_phase(context, cycle), expr.not_(context.bit("PREADY", cycle)))


def _held_control(context, cycle):
    """Every requester control signal that exists is stable from ``cycle`` to ``cycle+1``."""
    terms = []
    for name in STABLE_CONTROL:
        if context.has(name):
            terms.append(context.stable(name, cycle))
    if context.has("PWDATA"):
        # PWDATA only has to hold on a write transfer.
        terms.append(expr.implies(context.bit("PWRITE", cycle), context.stable("PWDATA", cycle)))
    return expr.and_(*terms)


def _transfer_continues(context, cycle):
    return expr.and_(context.bit("PSEL", cycle + 1), context.bit("PENABLE", cycle + 1))


def _ready_horizon(context):
    return int(context.option("max_wait_states", 8))


def _ready_within_bound(context, cycle):
    """``PREADY`` (or a reset that aborts the transfer) within the wait budget."""
    window = _ready_horizon(context)
    return expr.or_(
        *[
            expr.or_(
                context.bit("PREADY", cycle + offset),
                context.reset_active(cycle + offset),
            )
            for offset in range(window + 1)
        ]
    )


# ---------------------------------------------------------------------------
# the catalogue
# ---------------------------------------------------------------------------

PROPERTIES = (
    Property(
        "apb/requester/reset_inactive",
        "no transfer is started while PRESETn is asserted",
        obligation_of="requester",
        min_revision="APB2",
        kind="reset",
        horizon=0,
        requires=("PRESETn", "PSEL", "PENABLE"),
        antecedent=lambda ctx, t: ctx.reset_active(t),
        consequent=lambda ctx, t: expr.and_(
            expr.not_(ctx.bit("PSEL", t)), expr.not_(ctx.bit("PENABLE", t))
        ),
        description=(
            "While the bus is in reset the requester must drive PSEL and PENABLE low, so "
            "that the first cycle after reset is a clean IDLE."
        ),
    ),
    Property(
        "apb/requester/enable_requires_select",
        "PENABLE is never asserted without PSEL",
        obligation_of="requester",
        min_revision="APB2",
        kind="sequencing",
        horizon=0,
        requires=("PSEL", "PENABLE"),
        antecedent=lambda ctx, t: ctx.bit("PENABLE", t),
        consequent=lambda ctx, t: ctx.bit("PSEL", t),
        description="PENABLE only qualifies an already selected transfer; it has no meaning alone.",
    ),
    Property(
        "apb/requester/setup_to_access",
        "a SETUP cycle is followed by exactly one ACCESS cycle",
        obligation_of="requester",
        min_revision="APB2",
        kind="sequencing",
        horizon=1,
        requires=("PSEL", "PENABLE"),
        antecedent=lambda ctx, t: expr.and_(_setup_phase(ctx, t), _running(ctx, t, 1)),
        consequent=lambda ctx, t: _transfer_continues(ctx, t),
        description=(
            "PSEL high with PENABLE low is the SETUP phase; the very next cycle must be the "
            "ACCESS phase of the same transfer (PSEL and PENABLE both high). A requester may "
            "not linger in SETUP or abandon the transfer."
        ),
    ),
    Property(
        "apb/requester/control_stable_setup_to_access",
        "address and control are stable from SETUP into ACCESS",
        obligation_of="requester",
        min_revision="APB2",
        kind="stability",
        horizon=1,
        requires=("PSEL", "PENABLE", "PADDR", "PWRITE"),
        antecedent=lambda ctx, t: expr.and_(_setup_phase(ctx, t), _running(ctx, t, 1)),
        consequent=_held_control,
        description=(
            "PADDR, PWRITE and (on a write) PWDATA -- plus PPROT/PSTRB from APB4 -- are driven "
            "in SETUP and must not change on the way into ACCESS."
        ),
    ),
    Property(
        "apb/requester/access_exit",
        "the ACCESS phase ends in the cycle after PREADY",
        obligation_of="requester",
        min_revision="APB3",
        kind="sequencing",
        horizon=1,
        requires=("PSEL", "PENABLE", "PREADY"),
        antecedent=lambda ctx, t: expr.and_(
            _access_phase(ctx, t), ctx.bit("PREADY", t), _running(ctx, t, 1)
        ),
        consequent=lambda ctx, t: expr.not_(ctx.bit("PENABLE", t + 1)),
        description=(
            "The transfer completes on the rising edge at which PREADY is high, so PENABLE must "
            "be deasserted in the following cycle (PSEL may stay high for a back-to-back "
            "transfer)."
        ),
    ),
    Property(
        "apb/requester/access_exit_single_cycle",
        "the ACCESS phase is exactly one cycle long (APB2 has no PREADY)",
        obligation_of="requester",
        min_revision="APB2",
        max_revision="APB2",
        kind="sequencing",
        horizon=1,
        requires=("PSEL", "PENABLE"),
        antecedent=lambda ctx, t: expr.and_(_access_phase(ctx, t), _running(ctx, t, 1)),
        consequent=lambda ctx, t: expr.not_(ctx.bit("PENABLE", t + 1)),
        description=(
            "APB2 has no wait states: every transfer takes exactly two cycles, so ACCESS is "
            "never extended."
        ),
    ),
    Property(
        "apb/requester/wait_hold",
        "the transfer is held while the completer is not ready",
        obligation_of="requester",
        min_revision="APB3",
        kind="sequencing",
        horizon=1,
        requires=("PSEL", "PENABLE", "PREADY"),
        antecedent=lambda ctx, t: expr.and_(_wait_state(ctx, t), _running(ctx, t, 1)),
        consequent=lambda ctx, t: _transfer_continues(ctx, t),
        description=(
            "A wait state (ACCESS with PREADY low) must not be abandoned: PSEL and PENABLE stay "
            "high until the completer accepts the transfer."
        ),
    ),
    Property(
        "apb/requester/control_stable_wait",
        "address and control are stable across wait states",
        obligation_of="requester",
        min_revision="APB3",
        kind="stability",
        horizon=1,
        requires=("PSEL", "PENABLE", "PREADY", "PADDR", "PWRITE"),
        antecedent=lambda ctx, t: expr.and_(_wait_state(ctx, t), _running(ctx, t, 1)),
        consequent=_held_control,
        description=(
            "While the completer inserts wait states the requester must hold PADDR, PWRITE and "
            "(on a write) PWDATA -- plus PPROT/PSTRB from APB4 -- unchanged. This is the "
            "property a wait-state bug in a requester breaks."
        ),
    ),
    Property(
        "apb/completer/ready_within_bound",
        "PREADY is asserted within the configured wait-state budget",
        obligation_of="completer",
        min_revision="APB3",
        kind="liveness",
        horizon=_ready_horizon,
        requires=("PSEL", "PENABLE", "PREADY"),
        antecedent=lambda ctx, t: expr.and_(_access_phase(ctx, t), _running(ctx, t)),
        consequent=_ready_within_bound,
        description=(
            "APB itself places no upper bound on wait states, so this is a *bounded* liveness "
            "obligation against the 'max_wait_states' budget the mapping declares. A reset "
            "inside the window discharges it, because the transfer is aborted."
        ),
        assumable=False,
    ),
    Property(
        "apb/completer/slverr_requires_ready",
        "PSLVERR is only asserted in the completing cycle of a transfer",
        obligation_of="completer",
        min_revision="APB3",
        kind="sequencing",
        horizon=0,
        requires=("PSEL", "PENABLE", "PREADY", "PSLVERR"),
        antecedent=lambda ctx, t: ctx.bit("PSLVERR", t),
        consequent=lambda ctx, t: expr.and_(_access_phase(ctx, t), ctx.bit("PREADY", t)),
        description=(
            "PSLVERR is only sampled in the last cycle of the ACCESS phase, when PSEL, PENABLE "
            "and PREADY are all high. Driving it during a wait state reports an error for a "
            "transfer that has not completed."
        ),
    ),
    Property(
        "apb/completer/ready_low_when_deselected",
        "PREADY is low while the completer is not selected",
        obligation_of="completer",
        min_revision="APB3",
        kind="sequencing",
        horizon=0,
        requires=("PSEL", "PREADY"),
        antecedent=lambda ctx, t: expr.not_(ctx.bit("PSEL", t)),
        consequent=lambda ctx, t: expr.not_(ctx.bit("PREADY", t)),
        description=(
            "PREADY is a don't-care while PSEL is low, so this is a house rule rather than an "
            "APB requirement. It is off unless 'check_ready_low_when_deselected' is set."
        ),
        severity="low",
        optional=True,
        assumable=False,
    ),
)

#: Things this checker knowingly does not do, reported as ``unsupported`` findings
#: whenever the run is in a configuration where a reader might expect them.
EXCLUSIONS = (
    {
        "id": "apb/exclusion/rdata_value",
        "kind": "construct",
        "title": "PRDATA/PWDATA payload correctness is not checked",
        "reason": (
            "Judging the value a completer returns needs a reference model of the register "
            "map, which this analysis does not have. Only the protocol framing around the "
            "data phase is checked, never the data itself."
        ),
        "applies": "always",
    },
    {
        "id": "apb/exclusion/pstrb_pprot_semantics",
        "kind": "construct",
        "title": "PSTRB and PPROT semantics are not checked",
        "reason": (
            "PSTRB byte-lane meaning and PPROT protection encodings are checked only for "
            "stability, not for legality (for example PSTRB must be low on a read)."
        ),
        "applies": "APB4",
    },
    {
        "id": "apb/exclusion/apb5",
        "kind": "configuration",
        "title": "APB5 signalling is out of scope",
        "reason": (
            "PWAKEUP, parity/RME and user signalling are APB5 additions. Selecting an APB5 "
            "revision is rejected outright rather than silently checked as APB4."
        ),
        "applies": "always",
    },
    {
        "id": "apb/exclusion/axi_lite",
        "kind": "configuration",
        "title": "AXI4-Lite is a follow-up, not a supported protocol",
        "reason": (
            "AXI4-Lite has independent channels and handshakes that share no properties with "
            "APB; it is tracked as a follow-up rather than approximated here."
        ),
        "applies": "always",
    },
    {
        "id": "apb/exclusion/auto_discovery",
        "kind": "configuration",
        "title": "bus signals are user-mapped, never discovered",
        "reason": (
            "Every signal comes from the explicit mapping file. Nothing is inferred from net "
            "names, so a wrong mapping produces a wrong answer -- validate the mapping against "
            "the netlist before trusting a result."
        ),
        "applies": "always",
    },
    {
        "id": "apb/exclusion/sub_cycle",
        "kind": "construct",
        "title": "sub-cycle behaviour is outside the model",
        "reason": (
            "The transition system has one step per active PCLK edge. Clock trees, gated or "
            "divided clocks, multi-clock crossings, setup/hold, glitches and X-propagation are "
            "therefore invisible to every result reported here."
        ),
        "applies": "always",
    },
    {
        "id": "apb/exclusion/multi_completer",
        "kind": "configuration",
        "title": "only one PSEL is modelled",
        "reason": (
            "Address decoding across several PSELx lines is not modelled; map one completer's "
            "PSEL per run."
        ),
        "applies": "always",
    },
)


def properties_for(revision, include_optional=False):
    """Every property that exists in ``revision``."""
    return tuple(
        prop
        for prop in PROPERTIES
        if prop.applies_to_revision(revision) and (include_optional or not prop.optional)
    )


def obligations_for(role, revision, include_optional=False):
    """Properties the DUT in ``role`` must satisfy."""
    return tuple(
        prop for prop in properties_for(revision, include_optional) if prop.obligation_of == role
    )


def assumptions_for(role, revision):
    """Properties assumed of the *environment* when the DUT plays ``role``.

    These are the other side's obligations. They are constraints on the search,
    which is exactly why they are reported alongside every finding and why a
    contradictory set is flagged as overconstrained instead of proving things.
    """
    other = "completer" if role == "requester" else "requester"
    return tuple(
        prop for prop in properties_for(revision, include_optional=False)
        if prop.obligation_of == other and prop.assumable
    )
