#!/usr/bin/env python3
"""Acceptance tests for the feature issues #52, #55 and #57.

Each test below says what "done" means for one issue, in the form the reviewer
of that issue will check.  A test for an issue that is still open is marked
``unittest.expectedFailure``: the suite stays green while the feature is open,
and turns red -- loudly, as an *unexpected success* -- on the day the feature
lands and the marker has to come off.  A marker that has to be deleted is the
point: it forces someone to look at the assertion and confirm it is the right
one before the issue is closed.

#52 (``get_gate_by_name`` / ``get_net_by_name``) and #57 (the location
warning) have landed and their markers are gone; the assertions stayed exactly
as they were written while the issues were open.  #55 is still open.

Everything that is not inside an expected failure is a fact the walkthroughs
already publish (``examples/agilex3_walkthroughs/*/check.py`` re-checks the
same numbers against the same committed netlists).  Those grounding tests pass
today and must keep passing: they are what distinguishes "the feature is still
missing" from "the test is wrong", and they stop an expected failure from
failing for a reason that has nothing to do with its issue.

The fourth issue of this batch, #56 (``hal_fsm`` ``propose()``/``rank()``
ergonomics), is a pure-Python API question and lives with the code it tests, in
``tools/hal_fsm/test_hal_fsm.py`` (class ``ProposeRankErgonomicsTest``).  It
needs no HAL build and is not duplicated here.

Run this file inside the HAL build container, from the repository root::

    HAL_BASE_PATH=/work/build PYTHONPATH=/work/build/lib \\
        python3 tests/acceptance/test_feature_acceptance.py

``python3 -m unittest`` and ``pytest`` work too.  ``hal_py`` has to be
importable: a missing build is a hard failure here, not a skip, because an
acceptance suite that quietly turns itself off would let #52, #55 and #57 land
-- or rot -- unnoticed.
"""

import contextlib
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
WALKTHROUGHS = os.path.join(REPO, "examples", "agilex3_walkthroughs")

GATE_LIBRARY = os.path.join(
    REPO, "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl"
)
BLINKY_NETLIST = os.path.join(WALKTHROUGHS, "01_blinky_counter", "netlist.hal.v")
ACCUMULATOR_NETLIST = os.path.join(
    WALKTHROUGHS, "06_accumulator_alu", "netlist", "netlist.hal.v"
)

# ---------------------------------------------------------------------------
# Facts about the committed netlists, all of them re-checked by the
# walkthroughs' own check.py scripts.  If one of these stops holding, the
# netlist was regenerated or HAL started reading it differently -- either way
# the expected failures below stop meaning what they say.
# ---------------------------------------------------------------------------

#: 01_blinky_counter: 24 ``tennm_ff`` + 24 ``tennm_lcell_comb`` + gnd + vcc.
BLINKY_GATE_COUNT = 50
#: The bit-0 flip-flop of the counter.  The Verilog instance is ``\count[0]``.
BLINKY_GATE_NAME = "count[0]"
#: The net that flip-flop drives.  Quartus calls it ``count(0)`` -- note that
#: ``count[0]~0_combout`` is a *different* net and ``count[0]`` is not a net at
#: all, which is exactly why the lookup #52 asks for is specified as an
#: exact match returning ``None`` on a miss.
BLINKY_NET_NAME = "count(0)"

#: 06_accumulator_alu: one register bank, nine flip-flops (``acc[0..7]~reg0``
#: plus ``carry~reg0``) sharing one opcode-decoded enable and one async clear.
ACCUMULATOR_FF_COUNT = 9
#: The eight accumulator output nets -- "the acc register bank" for #55.
ACC_REGISTER_NETS = frozenset("acc({})".format(bit) for bit in range(8))


# ---------------------------------------------------------------------------
# HAL access
# ---------------------------------------------------------------------------

_HAL = []
_NETLISTS = {}


def hal():
    """Import ``hal_py`` once and register the parser plugins.

    Without ``load_all_plugins`` the ``.hgl`` and ``.v`` parsers are not
    registered and every load silently returns ``None``.
    """
    if not _HAL:
        for entry in os.environ.get("HAL_PY_PATH", "").split(os.pathsep):
            if entry and entry not in sys.path:
                sys.path.insert(0, entry)
        try:
            import hal_py
        except ImportError as exc:  # pragma: no cover - environment problem
            raise AssertionError(
                "hal_py is not importable ({}); these are acceptance tests for "
                "HAL features and need a build:\n"
                "    HAL_BASE_PATH=<build> PYTHONPATH=<build>/lib python3 "
                "tests/acceptance/test_feature_acceptance.py".format(exc)
            )
        hal_py.plugin_manager.load_all_plugins()
        _HAL.append(hal_py)
    return _HAL[0]


def load_netlist(path):
    """A fresh load of ``path`` against the Agilex gate library."""
    netlist = hal().NetlistFactory.load_netlist(path, GATE_LIBRARY)
    if netlist is None:
        raise AssertionError("could not load {} with {}".format(path, GATE_LIBRARY))
    return netlist


def netlist_for(path):
    """A shared load, for the tests that only read it."""
    if path not in _NETLISTS:
        _NETLISTS[path] = load_netlist(path)
    return _NETLISTS[path]


@contextlib.contextmanager
def captured_log_output():
    """Capture what HAL writes to file descriptors 1 and 2.

    HAL's logger goes through spdlog to the process' ``stdout``, not through
    ``sys.stdout``, so ``contextlib.redirect_stdout`` sees nothing at all.
    Redirecting the file descriptors with ``os.dup2`` is the same technique
    ``tools/hal_capabilities/cli.py``'s ``main()`` uses, and for the same
    reason.  The captured text lands in ``["text"]`` when the block exits.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    sink = tempfile.TemporaryFile(mode="w+b")
    saved_out, saved_err = os.dup(1), os.dup(2)
    captured = {"text": ""}
    try:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        yield captured
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
        sink.seek(0)
        captured["text"] = sink.read().decode("utf-8", "replace")
        sink.close()


# ---------------------------------------------------------------------------
# issue #52 -- Netlist.get_gate_by_name / get_net_by_name
# ---------------------------------------------------------------------------


class NameLookupAcceptanceTest(unittest.TestCase):
    """#52: ``Netlist`` had no lookup by name.

    Every walkthrough script re-invented a linear scan over ``get_gates()``;
    02_traffic_fsm's runner called ``netlist.get_gate_by_name(...)`` outright
    and crashed, because it did not exist.  #52 asked for exact-match
    ``get_gate_by_name`` / ``get_net_by_name`` returning ``None`` on a miss
    (and a plural ``get_gates_by_name`` for the duplicate-name case, which is
    not pinned here -- the two singular lookups are what the walkthroughs
    actually reach for; the duplicate-name contract is pinned by the C++ unit
    tests in ``tests/netlist/netlist.cpp``).
    """

    @classmethod
    def setUpClass(cls):
        cls.netlist = netlist_for(BLINKY_NETLIST)

    def test_the_names_being_looked_up_are_unambiguous(self):
        """Grounding: the scan every script writes today, and what it finds."""
        gates = [
            gate
            for gate in self.netlist.get_gates()
            if gate.get_name() == BLINKY_GATE_NAME
        ]
        nets = [
            net for net in self.netlist.get_nets() if net.get_name() == BLINKY_NET_NAME
        ]
        self.assertEqual(len(self.netlist.get_gates()), BLINKY_GATE_COUNT)
        self.assertEqual([gate.get_name() for gate in gates], [BLINKY_GATE_NAME])
        self.assertEqual([net.get_name() for net in nets], [BLINKY_NET_NAME])
        # One gate, one net, and the net is that gate's q output: the lookup
        # has exactly one right answer, so there is nothing to design around.
        self.assertEqual(gates[0].get_type().get_name(), "tennm_ff")
        self.assertEqual(
            [net.get_name() for net in gates[0].get_fan_out_nets()],
            [BLINKY_NET_NAME],
        )

    def test_a_gate_name_is_not_a_net_name(self):
        """Grounding for the ``None``-on-a-miss half of #52's contract."""
        self.assertNotIn(
            BLINKY_GATE_NAME, [net.get_name() for net in self.netlist.get_nets()]
        )

    def test_get_gate_by_name(self):
        # #52, implemented: Netlist::get_gate_by_name, an exact match over
        # get_gates() returning None on a miss and on an ambiguous name.
        gate = self.netlist.get_gate_by_name(BLINKY_GATE_NAME)
        self.assertIsNotNone(gate, "exact-match lookup missed " + BLINKY_GATE_NAME)
        self.assertEqual(gate.get_name(), BLINKY_GATE_NAME)
        self.assertEqual(gate.get_type().get_name(), "tennm_ff")
        self.assertIsNone(
            self.netlist.get_gate_by_name("count[0]~no_such_gate"),
            "a miss must be None, not an exception and not a partial match",
        )

    def test_get_net_by_name(self):
        # #52, implemented: same, for nets.
        net = self.netlist.get_net_by_name(BLINKY_NET_NAME)
        self.assertIsNotNone(net, "exact-match lookup missed " + BLINKY_NET_NAME)
        self.assertEqual(net.get_name(), BLINKY_NET_NAME)
        self.assertEqual(
            [source.get_gate().get_name() for source in net.get_sources()],
            [BLINKY_GATE_NAME],
        )
        self.assertIsNone(
            self.netlist.get_net_by_name(BLINKY_GATE_NAME),
            "exact match: count[0] is a gate name, there is no net by that name",
        )


# ---------------------------------------------------------------------------
# issue #55 -- module_identification on AGILEX_TENNM
# ---------------------------------------------------------------------------


class ModuleIdentificationAgilexAcceptanceTest(unittest.TestCase):
    """#55: ``module_identification`` does not dispatch on AGILEX_TENNM.

    06_accumulator_alu is a textbook 8-bit accumulator -- a ten-cell carry
    chain and an opcode-decoded enable -- and its walkthrough reports the null
    honestly (``artifacts/08_module_identification.txt``: "known registers
    handed to the plugin: 1 / module_identification.execute returned None"),
    because the plugin currently only knows Xilinx Unisim and Lattice iCE40
    gate libraries.  Reporting the null is the right thing for the walkthrough
    to do and the wrong thing for the plugin to produce; #55 is the second
    half.  The expected failure below is the positive recognition #55 asks
    for, over the register bank the walkthrough already identifies.
    """

    @classmethod
    def setUpClass(cls):
        hal()
        try:
            from hal_plugins import module_identification
        except ImportError as exc:  # pragma: no cover - build problem
            raise AssertionError(
                "the module_identification plugin is not importable ({}); "
                "#55 is about that plugin, so a missing build is a failure "
                "and not a skip".format(exc)
            )
        cls.module_identification = module_identification
        cls.netlist = netlist_for(ACCUMULATOR_NETLIST)

    def register_bank(self):
        """The one register bank of 06, as its own analysis hands it over."""
        return sorted(
            (
                gate
                for gate in self.netlist.get_gates()
                if gate.get_type().get_name() == "tennm_ff"
            ),
            key=lambda gate: gate.get_name(),
        )

    def execute(self):
        """``analyze.py``'s step 8, minus the reporting."""
        configuration = self.module_identification.Configuration(
            self.netlist
        ).with_known_registers([self.register_bank()])
        return self.module_identification.execute(configuration)

    def test_the_register_bank_is_the_one_the_walkthrough_reports(self):
        """Grounding: nine flip-flops, one enable, one clear, acc + carry."""
        bank = self.register_bank()
        self.assertEqual(len(bank), ACCUMULATOR_FF_COUNT)
        self.assertEqual(
            {gate.get_fan_in_net("ena").get_name() for gate in bank},
            {"i45~1_combout"},
        )
        self.assertEqual(
            {gate.get_fan_in_net("clrn").get_name() for gate in bank}, {"rst_n"}
        )
        driven = {
            net.get_name() for gate in bank for net in gate.get_fan_out_nets()
        }
        self.assertEqual(driven, set(ACC_REGISTER_NETS) | {"carry"})

    @unittest.expectedFailure
    def test_the_accumulator_is_recognised_as_an_addition(self):
        # #55: today execute() returns None (the plugin logs that the gate
        # library is not one it dispatches on) and the assertion below is the
        # first to fail.
        result = self.execute()
        self.assertIsNotNone(
            result,
            "module_identification.execute returned None for a plain 8-bit "
            "accumulator; #55 is about dispatching on AGILEX_TENNM",
        )

        addition = {
            self.module_identification.CandidateType.addition,
            self.module_identification.CandidateType.addition_offset,
        }
        candidates = list((result.get_verified_candidates() or {}).values())
        self.assertTrue(candidates, "a result with no verified candidate")

        over_the_bank = []
        for candidate in candidates:
            if not addition.intersection(set(candidate.types)):
                continue
            names = {net.get_name() for net in (candidate.output_nets or [])}
            for operand in candidate.operands or []:
                names.update(net.get_name() for net in operand)
            if ACC_REGISTER_NETS.issubset(names):
                over_the_bank.append(candidate)

        self.assertTrue(
            over_the_bank,
            "no addition-class candidate covers the acc register bank; got "
            + repr(
                [
                    (
                        candidate.get_name(),
                        sorted(str(kind) for kind in candidate.types),
                    )
                    for candidate in candidates
                ]
            ),
        )


# ---------------------------------------------------------------------------
# issue #57 -- 'failed to load locations of N gates' on every load
# ---------------------------------------------------------------------------


class LocationWarningAcceptanceTest(unittest.TestCase):
    """#57: every walkthrough load warned about locations that never existed.

    ``Netlist::load_gate_locations_from_data`` is called by the Verilog parser
    on every import and warned once per load with the full gate count, because
    Quartus ``.vo`` exports carry no placement at all.  A warning that fires
    for the normal case teaches readers to ignore warnings.  #57 asked for it
    to be demoted when *no* gate has location data, and kept when some do and
    others unexpectedly do not -- which is what it does now.
    """

    WARNING = "failed to load locations"

    def test_no_gate_in_this_netlist_has_location_data(self):
        """Grounding: this is the all-missing case, not the mixed case."""
        netlist = netlist_for(BLINKY_NETLIST)
        located = [gate for gate in netlist.get_gates() if gate.has_location()]
        self.assertEqual(len(netlist.get_gates()), BLINKY_GATE_COUNT)
        self.assertEqual(located, [], "expected a netlist with no placement at all")

    def test_the_log_capture_itself_works(self):
        """Grounding for the expected failure: absence has to mean absence.

        If the fd capture silently returned nothing, the assertion in
        ``test_no_location_warning_for_a_netlist_without_locations`` would
        *pass* and report an unexpected success -- a false claim that #57 is
        fixed.  So the capture is tested on its own: a direct write to fd 1,
        and HAL's own parser chatter from a real load.
        """
        hal()  # plugin-loading chatter stays outside the capture
        with captured_log_output() as log:
            os.write(1, b"marker-written-to-fd-1\n")
            load_netlist(BLINKY_NETLIST)
        text = log["text"]
        self.assertIn("marker-written-to-fd-1", text)
        self.assertIn("[netlist_parser] [info]", text)

    def test_no_location_warning_for_a_netlist_without_locations(self):
        # #57, implemented: the all-missing case is logged at debug level now;
        # only a *partial* miss still warns.
        hal()
        with captured_log_output() as log:
            load_netlist(BLINKY_NETLIST)
        text = log["text"]
        if "[netlist_parser]" not in text:  # pragma: no cover - capture failed
            self.skipTest(
                "could not capture HAL's log output; refusing to report the "
                "absence of a warning as evidence"
            )
        self.assertNotIn(
            self.WARNING,
            text,
            "a netlist with no location data at all is the normal case for a "
            "Quartus .vo import and must not warn",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
