"""Standalone unit tests for hal_bitstream.

They run on a plain interpreter: no HAL build, no FPGA toolchain, no device. Two things stand in
for the real converters:

* **stub converters** -- small executables written into a temp directory and passed with
  ``--converter NAME=PATH``. They exercise the whole chain (detection, planning, the two-step
  iCE40 pipeline, stdout capture, failure handling, the provenance manifest) deterministically, on
  any machine.
* **recorded converter output** -- ``fixtures/ice40_blinky.v`` is what the *real* iCE40 chain
  (IceStorm's ``iceunpack`` and ``icebox_vlog``, then ``yosys -p synth_ice40``) produced for the
  bitstream in ``fixtures/``, and ``fixtures/ice40_blinky.icebox_vlog.v`` is the behavioural
  intermediate ``icebox_vlog`` wrote on the way (see fixtures/README.md). The end-to-end run that
  produced them (yosys -> nextpnr-ice40 -> icepack -> iceunpack -> icebox_vlog -> yosys -> hal_py)
  is in tests/headless_smoke/bitstream_smoke.py; what is checked here is what can be checked
  without the toolchain: that the netlist instantiates only primitives the shipped iCE40 gate
  library defines, which is the promise 'produces a Verilog netlist plus the correct gate library
  reference', and that ``icebox_vlog``'s own output is *not* such a netlist, which is why the
  chain does not stop there.

    python -m unittest discover -s tools/hal_bitstream -t tools -p "test_*.py"
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_bitstream import cli, convert, detect, registry  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
REPO_ROOT = registry.REPO_ROOT
ICE40_LIBRARY = os.path.join(
    REPO_ROOT, "plugins", "gate_libraries", "definitions", "ice40ultra.hgl"
)

# The first bytes of a real icepack output: the comment block, then the iCE40 sync word.
ICE40_BIN_HEAD = b"\xff\x00Part: iCE40UP5K-SG48\x00\xff\x00\x00\xff" + b"\x7e\xaa\x99\x7e"
ICE40_ASC = ".comment icepack\n.device 5k\n.io_tile 0 0\n"
ECP5_BIT_HEAD = b"LFE5U-25F\x00\xff\xff\xff\xff\xbd\xb3"
XILINX_BIT_HEAD = b"\x00\x09\x0f\xf0\x0f\xf0" + b"\xff" * 32 + b"\xaa\x99\x55\x66"
GOWIN_FS = "//Gowin FPGA Bitstream\n0101010101\n"


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def write_stub(directory, name, python_body):
    """A fake converter that runs ``python_body``; returns the path to pass to --converter."""
    script = os.path.join(directory, name + ".py")
    with open(script, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("import sys, os\n")
        handle.write(python_body)
    if sys.platform == "win32":
        launcher = os.path.join(directory, name + ".bat")
        with open(launcher, "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write('@echo off\r\n"{}" "{}" %*\r\n'.format(sys.executable, script))
        return launcher
    launcher = os.path.join(directory, name)
    with open(launcher, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("#!/bin/sh\nexec {} {} \"$@\"\n".format(sys.executable, script))
    os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


ICEUNPACK_STUB = """
if "--help" in sys.argv:
    print("iceunpack (stub)")
    sys.exit(0)
source, target = sys.argv[1], sys.argv[2]
data = open(source, "rb").read()
if b"\\x7e\\xaa\\x99\\x7e" not in data:
    sys.stderr.write("Error: not an iCE40 bitstream\\n")
    sys.exit(1)
open(target, "w").write(".comment stub\\n.device 5k\\n.logic_tile 1 1\\n")
"""

# icebox_vlog writes *behavioural* Verilog to stdout (that is the real tool's output shape: LUT
# equations and always blocks, not instances), and takes the bitstream as its last argument.
ICEBOX_VLOG_STUB = """
if "--help" in sys.argv:
    print("icebox_vlog (stub)")
    sys.exit(0)
text = open(sys.argv[-1]).read()
if ".device" not in text:
    sys.stderr.write("Error: not an icebox ASCII file\\n")
    sys.exit(2)
sys.stdout.write('''module chip (input a, output y);
  wire n1;
  reg n2 = 0;
  assign n1 = /* LUT    1  1  0 */ !a;
  /* FF  1  1  0 */ always @(posedge a) n2 <= n1;
  assign y = n2;
endmodule
''')
"""

# yosys is driven with '-q -p "<script>"'; the stub reads the script the same way the real one
# does, so a change to the command line in the registry breaks the stub too.
YOSYS_STUB = """
import re
if "-V" in sys.argv or "--version" in sys.argv:
    print("Yosys 0.33 (stub)")
    sys.exit(0)
script = sys.argv[sys.argv.index("-p") + 1]
source = re.search(r"read_verilog (\\S+);", script).group(1)
target = re.search(r"write_verilog\\s+(?:-\\S+\\s+)*(\\S+)", script).group(1)
text = open(source).read()
if "module" not in text:
    sys.stderr.write("ERROR: no module in %s\\n" % source)
    sys.exit(1)
open(target, "w").write('''module chip(a, y);
  input a;
  output y;
  wire n1;
  SB_LUT4 #(
    .LUT_INIT(16'h5555)
  ) _01_ (
    .I0(a), .I1(1'b0), .I2(1'b0), .I3(1'b0), .O(n1));
  SB_DFF _02_ (
    .C(a), .D(n1), .Q(y));
endmodule
''')
"""

# IceStorm's converters are Python scripts and greet every invocation with SyntaxWarnings; the
# manifest must record what the program says about itself, not the interpreter's complaints.
NOISY_VERSION_STUB = """
sys.stderr.write("noisy:238: SyntaxWarning: invalid escape sequence\\n")
sys.stderr.write("  match = re_match_cached(pattern)\\n")
sys.stderr.flush()
sys.stdout.write("\\nUsage: noisy [options] [input-file]\\n")
"""

FAILING_STUB = """
sys.stderr.write("Error: the bitstream is for a different device\\n")
sys.exit(3)
"""

EMPTY_OUTPUT_STUB = """
if "--help" in sys.argv:
    print("stub")
sys.exit(0)
"""


class TempCaseMixin(object):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hal_bitstream_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, content):
        path = os.path.join(self.tmp, name)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode) as handle:
            handle.write(content)
        return path

    def hide_toolchain(self):
        """Empty ``PATH`` for the duration of the test.

        The 'no converter is installed' tests must say the same thing on a developer laptop and in
        a container that has IceStorm and yosys installed (the one this fork's smoke test needs),
        so they take the toolchain away instead of hoping it is absent.
        """
        empty = os.path.join(self.tmp, "empty_path")
        if not os.path.isdir(empty):
            os.makedirs(empty)
        previous = os.environ.get("PATH")
        os.environ["PATH"] = empty
        self.addCleanup(os.environ.__setitem__, "PATH", previous if previous is not None else "")


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


class RegistryTest(unittest.TestCase):
    def test_every_family_is_described(self):
        for family in registry.families():
            self.assertTrue(family.name and family.vendor and family.toolchain, family.key)
            self.assertIn(
                family.status,
                (registry.STATUS_VERIFIED, registry.STATUS_UNVERIFIED, registry.STATUS_NO_CONVERTER),
            )
            if family.status == registry.STATUS_NO_CONVERTER:
                self.assertFalse(family.has_converter, family.key)
                self.assertTrue(family.notes, "{} must say why it has no converter".format(family.key))
            else:
                self.assertTrue(family.has_converter, family.key)

    def test_named_gate_libraries_exist(self):
        for family in registry.families():
            path = family.gate_library_file()
            if path is not None:
                self.assertTrue(
                    os.path.isfile(path),
                    "{} names a gate library that is not in this checkout: {}".format(
                        family.key, path
                    ),
                )

    def test_ice40_is_wired_to_icestorm_and_the_ice40_library(self):
        family = registry.get("ice40")
        self.assertEqual(
            ["iceunpack", "icebox_vlog", "yosys"], [step.program for step in family.steps]
        )
        self.assertEqual("ice40ultra.hgl", family.gate_library)
        self.assertEqual(registry.STATUS_VERIFIED, family.status)

    def test_every_step_declares_how_to_get_the_program(self):
        # A converter that is missing has to be actionable, which is only possible if the step
        # says where the program comes from.
        for family in registry.families():
            for step in family.steps:
                self.assertTrue(step.purpose, "{}/{}".format(family.key, step.program))
                self.assertTrue(
                    step.url or step.package,
                    "{}/{} says nothing about where to get it".format(family.key, step.program),
                )

    def test_unknown_family_lists_the_known_ones(self):
        with self.assertRaises(registry.UnknownFamilyError) as caught:
            registry.get("spartan6")
        self.assertIn("ice40", str(caught.exception))

    def test_registry_is_extensible(self):
        family = registry.Family(
            key="test_family",
            name="Test",
            vendor="Test",
            toolchain="Test toolchain",
            extensions=(".tst",),
            steps=(registry.ConverterStep(program="tst_vlog", output_suffix=".v"),),
        )
        registry.register(family)
        self.addCleanup(registry._REGISTRY.pop, "test_family", None)
        self.addCleanup(registry._ORDER.remove, "test_family")
        self.assertIs(family, registry.get("test_family"))
        self.assertIn(family, registry.families_for_extension(".TST"))
        with self.assertRaises(ValueError):
            registry.register(family)
        registry.register(family, replace=True)

    def test_register_rejects_non_families(self):
        with self.assertRaises(TypeError):
            registry.register("ice40")


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


class DetectionTest(TempCaseMixin, unittest.TestCase):
    def test_ice40_binary_is_detected_by_its_sync_word(self):
        path = self.write("blinky.bin", ICE40_BIN_HEAD + b"\x00" * 64)
        detection = detect.sniff(path)
        self.assertEqual("ice40", detection.family.key)
        self.assertEqual("magic", detection.method)
        self.assertIn("7E AA 99 7E", detection.evidence)

    def test_icebox_ascii_is_detected(self):
        path = self.write("blinky.asc", ICE40_ASC)
        self.assertEqual("ice40", detect.sniff(path).family.key)

    def test_ecp5_is_detected_by_its_preamble(self):
        path = self.write("top.bit", ECP5_BIT_HEAD + b"\x00" * 64)
        self.assertEqual("ecp5", detect.sniff(path).family.key)

    def test_xilinx_is_detected_and_reported_as_unconvertible(self):
        path = self.write("design.bit", XILINX_BIT_HEAD + b"\x00" * 64)
        detection = detect.sniff(path)
        self.assertEqual("xilinx7", detection.family.key)
        with self.assertRaises(convert.MissingConverterError) as caught:
            convert.plan(detection.family, path, os.path.join(self.tmp, "out.v"))
        message = str(caught.exception)
        self.assertIn("no open bitstream-to-netlist converter", message)
        self.assertIn("prjxray", message)

    def test_gowin_is_detected(self):
        path = self.write("top.fs", GOWIN_FS)
        self.assertEqual("gowin", detect.sniff(path).family.key)

    def test_unknown_content_with_an_ambiguous_extension_asks_for_a_family(self):
        path = self.write("mystery.bit", b"\x01\x02\x03\x04" * 64)
        with self.assertRaises(detect.DetectionError) as caught:
            detect.sniff(path)
        message = str(caught.exception)
        self.assertIn("--family", message)
        self.assertIn("ambiguous", message)

    def test_unknown_content_and_extension_lists_the_known_families(self):
        path = self.write("mystery.dat", b"\x01\x02\x03\x04" * 64)
        with self.assertRaises(detect.DetectionError) as caught:
            detect.sniff(path)
        message = str(caught.exception)
        for key in ("ice40", "ecp5", "xilinx7"):
            self.assertIn(key, message)

    def test_missing_and_empty_files_are_reported(self):
        with self.assertRaises(detect.DetectionError):
            detect.sniff(os.path.join(self.tmp, "nope.bin"))
        empty = self.write("empty.bin", b"")
        with self.assertRaises(detect.DetectionError) as caught:
            detect.sniff(empty)
        self.assertIn("empty", str(caught.exception))

    def test_forced_family_reports_a_mismatch_instead_of_hiding_it(self):
        path = self.write("design.bit", XILINX_BIT_HEAD + b"\x00" * 64)
        detection = detect.detect_family(path, "ice40")
        self.assertEqual("ice40", detection.family.key)
        self.assertEqual("forced-mismatch", detection.method)
        self.assertIn("xilinx7", detection.evidence)

    def test_forced_family_on_an_unrecognised_file_is_allowed(self):
        path = self.write("stripped.dat", b"\x00" * 128)
        detection = detect.detect_family(path, "ice40")
        self.assertEqual("forced", detection.method)

    def test_forced_unknown_family_raises(self):
        path = self.write("blinky.bin", ICE40_BIN_HEAD)
        with self.assertRaises(registry.UnknownFamilyError):
            detect.detect_family(path, "not_a_family")


# ---------------------------------------------------------------------------
# planning and conversion
# ---------------------------------------------------------------------------


class ConversionTest(TempCaseMixin, unittest.TestCase):
    def stubs(self, iceunpack=ICEUNPACK_STUB, icebox_vlog=ICEBOX_VLOG_STUB, yosys=YOSYS_STUB):
        directory = os.path.join(self.tmp, "bin")
        if not os.path.isdir(directory):
            os.makedirs(directory)
        overrides = {}
        for name, body in (
            ("iceunpack", iceunpack),
            ("icebox_vlog", icebox_vlog),
            ("yosys", yosys),
        ):
            if body is not None:
                overrides[name] = write_stub(directory, name, body)
        return overrides

    def test_missing_converter_names_the_program_and_how_to_get_it(self):
        self.hide_toolchain()
        path = self.write("blinky.bin", ICE40_BIN_HEAD)
        with self.assertRaises(convert.MissingConverterError) as caught:
            convert.plan(registry.get("ice40"), path, os.path.join(self.tmp, "out.v"), {})
        message = str(caught.exception)
        self.assertIn("iceunpack", message)
        self.assertIn("Project IceStorm", message)
        self.assertIn("fpga-icestorm", message)
        self.assertIn("--converter iceunpack=", message)

    def test_plan_skips_the_unpack_step_for_an_already_unpacked_file(self):
        overrides = self.stubs()
        asc = self.write("blinky.asc", ICE40_ASC)
        planned = convert.plan(
            registry.get("ice40"), asc, os.path.join(self.tmp, "out.v"), overrides
        )
        self.assertEqual(["icebox_vlog", "yosys"], [step.step.program for step in planned])
        self.assertEqual(asc, planned[0].input_path)

    def test_plan_runs_every_step_for_a_packed_bitstream(self):
        overrides = self.stubs()
        binary = self.write("blinky.bin", ICE40_BIN_HEAD)
        planned = convert.plan(
            registry.get("ice40"), binary, os.path.join(self.tmp, "out.v"), overrides
        )
        self.assertEqual(
            ["iceunpack", "icebox_vlog", "yosys"], [step.step.program for step in planned]
        )
        self.assertTrue(planned[0].output_path.endswith(".asc"))
        self.assertEqual(planned[0].output_path, planned[1].input_path)
        self.assertEqual(planned[1].output_path, planned[2].input_path)
        self.assertEqual(os.path.join(self.tmp, "out.v"), planned[2].output_path)

    def test_a_step_that_consumes_the_previous_step_s_output_is_kept(self):
        # Applicability is decided against what a step is handed, not against the original file:
        # here the second step only accepts .asc, which only exists after the first step ran.
        overrides = self.stubs()
        family = registry.Family(
            key="chained_test_family",
            name="Chained",
            vendor="Test",
            toolchain="Test",
            extensions=(".bin",),
            steps=(
                registry.ConverterStep(
                    program="iceunpack",
                    arguments=("{input}", "{output}"),
                    output_suffix=".asc",
                    input_suffixes=(".bin",),
                ),
                registry.ConverterStep(
                    program="icebox_vlog",
                    output_suffix=".v",
                    capture_stdout=True,
                    input_suffixes=(".asc",),
                ),
            ),
        )
        binary = self.write("blinky.bin", ICE40_BIN_HEAD)
        planned = convert.plan(family, binary, os.path.join(self.tmp, "out.v"), overrides)
        self.assertEqual(["iceunpack", "icebox_vlog"], [step.step.program for step in planned])

    def test_conversion_writes_a_netlist_and_a_provenance_manifest(self):
        overrides = self.stubs()
        binary = self.write("blinky.bin", ICE40_BIN_HEAD + b"\x11" * 32)
        output = os.path.join(self.tmp, "blinky.v")
        result = convert.convert(binary, output=output, overrides=overrides)

        self.assertTrue(os.path.isfile(output))
        with open(output, encoding="utf-8") as handle:
            self.assertIn("SB_LUT4", handle.read())
        self.assertEqual(ICE40_LIBRARY, result.gate_library)

        manifest = result.manifest
        self.assertEqual("ice40", manifest["detection"]["family"])
        self.assertEqual(64, len(manifest["bitstream"]["sha256"]))
        self.assertEqual(os.path.getsize(binary), manifest["bitstream"]["size_bytes"])
        self.assertEqual(
            ["iceunpack", "icebox_vlog", "yosys"], [s["program"] for s in manifest["steps"]]
        )
        for step in manifest["steps"]:
            self.assertTrue(step["command"], step)
            self.assertTrue(os.path.isabs(step["resolved"]))
        self.assertEqual(manifest["netlist"]["path"], output)

        manifest_path = convert.write_manifest(manifest, os.path.join(self.tmp, "m.json"))
        with open(manifest_path, encoding="utf-8") as handle:
            self.assertEqual(manifest["bitstream"]["sha256"], json.load(handle)["bitstream"]["sha256"])

    def test_intermediates_do_not_pollute_the_output_directory(self):
        overrides = self.stubs()
        binary = self.write("blinky.bin", ICE40_BIN_HEAD)
        output_dir = os.path.join(self.tmp, "out")
        os.makedirs(output_dir)
        convert.convert(binary, output=os.path.join(output_dir, "b.v"), overrides=overrides)
        self.assertEqual(["b.v"], sorted(os.listdir(output_dir)))

    def test_keep_intermediates_writes_them_next_to_the_output(self):
        overrides = self.stubs()
        binary = self.write("blinky.bin", ICE40_BIN_HEAD)
        output_dir = os.path.join(self.tmp, "out")
        os.makedirs(output_dir)
        convert.convert(
            binary,
            output=os.path.join(output_dir, "b.v"),
            overrides=overrides,
            keep_intermediates=True,
        )
        self.assertTrue(any(name.endswith(".asc") for name in os.listdir(output_dir)))

    def test_a_failing_converter_reports_its_output_and_leaves_no_netlist(self):
        overrides = self.stubs(icebox_vlog=FAILING_STUB)
        binary = self.write("blinky.bin", ICE40_BIN_HEAD)
        output = os.path.join(self.tmp, "blinky.v")
        with self.assertRaises(convert.ConversionError) as caught:
            convert.convert(binary, output=output, overrides=overrides)
        message = str(caught.exception)
        self.assertIn("icebox_vlog", message)
        self.assertIn("exit code 3", message)
        self.assertIn("different device", message)
        self.assertFalse(os.path.exists(output), "a failed conversion left a netlist behind")

    def test_a_converter_that_writes_nothing_is_an_error(self):
        overrides = self.stubs(icebox_vlog=EMPTY_OUTPUT_STUB)
        binary = self.write("blinky.bin", ICE40_BIN_HEAD)
        with self.assertRaises(convert.ConversionError) as caught:
            convert.convert(binary, output=os.path.join(self.tmp, "b.v"), overrides=overrides)
        self.assertIn("empty", str(caught.exception))

    def test_a_wrong_family_reaches_the_converter_and_is_reported(self):
        overrides = self.stubs()
        xilinx = self.write("design.bit", XILINX_BIT_HEAD)
        with self.assertRaises(convert.ConversionError) as caught:
            convert.convert(
                xilinx, output=os.path.join(self.tmp, "x.v"), family="ice40", overrides=overrides
            )
        self.assertIn("iceunpack", str(caught.exception))

    def test_converter_overrides_are_validated(self):
        with self.assertRaises(convert.ConversionError):
            convert.parse_converter_overrides(["icebox_vlog"])
        with self.assertRaises(convert.ConversionError) as caught:
            convert.parse_converter_overrides(["icebox_vlog=" + os.path.join(self.tmp, "nope")])
        self.assertIn("no such file", str(caught.exception))
        overrides = self.stubs()
        parsed = convert.parse_converter_overrides(
            ["icebox_vlog=" + overrides["icebox_vlog"]]
        )
        self.assertEqual(overrides["icebox_vlog"], parsed["icebox_vlog"])

    def test_the_recorded_version_skips_interpreter_noise(self):
        directory = os.path.join(self.tmp, "bin")
        if not os.path.isdir(directory):
            os.makedirs(directory)
        noisy = write_stub(directory, "noisy", NOISY_VERSION_STUB)
        step = registry.ConverterStep(program="noisy", version_argument="--help")
        self.assertEqual("Usage: noisy [options] [input-file]", convert._program_version(noisy, step))

    def test_a_missing_gate_library_override_is_reported(self):
        overrides = self.stubs()
        binary = self.write("blinky.bin", ICE40_BIN_HEAD)
        with self.assertRaises(convert.ConversionError) as caught:
            convert.convert(
                binary,
                output=os.path.join(self.tmp, "b.v"),
                overrides=overrides,
                gate_library=os.path.join(self.tmp, "missing.hgl"),
            )
        self.assertIn("missing.hgl", str(caught.exception))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class CliTest(TempCaseMixin, unittest.TestCase):
    def run_cli(self, argv):
        import io

        stdout, stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            code = cli.main(argv)
            return code, sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.stdout, sys.stderr = stdout, stderr

    def test_families_json_lists_converter_availability(self):
        code, out, _ = self.run_cli(["families", "--json"])
        self.assertEqual(0, code)
        document = json.loads(out)
        keys = [entry["key"] for entry in document]
        self.assertIn("ice40", keys)
        ice40 = document[keys.index("ice40")]
        self.assertEqual("ice40ultra.hgl", ice40["gate_library"])
        self.assertEqual(
            ["iceunpack", "icebox_vlog", "yosys"], [c["program"] for c in ice40["converters"]]
        )
        self.assertIn("ready", ice40)

    def test_detect_json(self):
        path = self.write("blinky.bin", ICE40_BIN_HEAD)
        code, out, _ = self.run_cli(["detect", path, "--json"])
        self.assertEqual(0, code)
        self.assertEqual("ice40", json.loads(out)["family"])

    def test_detect_of_an_unknown_file_exits_2(self):
        path = self.write("mystery.dat", b"\x00" * 32)
        code, _, err = self.run_cli(["detect", path])
        self.assertEqual(cli.EXIT_ERROR, code)
        self.assertIn("not a bitstream", err)

    def test_convert_without_the_toolchain_exits_2_with_an_actionable_error(self):
        self.hide_toolchain()
        path = self.write("blinky.bin", ICE40_BIN_HEAD)
        code, _, err = self.run_cli(
            ["convert", path, "-o", os.path.join(self.tmp, "b.v"), "--quiet"]
        )
        self.assertEqual(cli.EXIT_ERROR, code)
        self.assertIn("iceunpack", err)
        self.assertIn("--converter", err)

    def test_convert_with_stub_converters_writes_netlist_and_manifest(self):
        self.hide_toolchain()
        directory = os.path.join(self.tmp, "bin")
        os.makedirs(directory)
        stubs = {
            "iceunpack": write_stub(directory, "iceunpack", ICEUNPACK_STUB),
            "icebox_vlog": write_stub(directory, "icebox_vlog", ICEBOX_VLOG_STUB),
            "yosys": write_stub(directory, "yosys", YOSYS_STUB),
        }
        path = self.write("blinky.bin", ICE40_BIN_HEAD)
        output = os.path.join(self.tmp, "blinky.v")
        argv = ["convert", path, "-o", output, "--quiet"]
        for name, stub in sorted(stubs.items()):
            argv += ["--converter", "{}={}".format(name, stub)]
        code, out, _ = self.run_cli(argv)
        self.assertEqual(0, code)
        printed = out.split()
        self.assertEqual(output, printed[0])
        with open(output, encoding="utf-8") as handle:
            self.assertIn("SB_LUT4", handle.read())
        with open(printed[1], encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual("ice40", manifest["detection"]["family"])
        self.assertTrue(manifest["gate_library"].endswith("ice40ultra.hgl"))
        self.assertEqual(
            ["iceunpack", "icebox_vlog", "yosys"], [s["program"] for s in manifest["steps"]]
        )


# ---------------------------------------------------------------------------
# recorded converter output
# ---------------------------------------------------------------------------


RECORDED_NETLIST = os.path.join(FIXTURES, "ice40_blinky.v")
RECORDED_BITSTREAM = os.path.join(FIXTURES, "ice40_blinky.bin")
RECORDED_MANIFEST = os.path.join(FIXTURES, "ice40_blinky.manifest.json")
RECORDED_ICEBOX_VLOG = os.path.join(FIXTURES, "ice40_blinky.icebox_vlog.v")

INSTANCE_RE = re.compile(r"^\s*(SB_[A-Z0-9_]+)\s", re.MULTILINE)


@unittest.skipUnless(
    os.path.isfile(RECORDED_NETLIST), "fixtures/ice40_blinky.v is not in this checkout"
)
class RecordedConverterOutputTest(unittest.TestCase):
    """The recorded output of a real run, checked against the shipped iCE40 gate library."""

    @classmethod
    def setUpClass(cls):
        with open(RECORDED_NETLIST, encoding="utf-8") as handle:
            cls.netlist = handle.read()
        with open(ICE40_LIBRARY, encoding="utf-8") as handle:
            cls.library = handle.read()

    def test_the_recorded_bitstream_is_detected_as_ice40(self):
        detection = detect.sniff(RECORDED_BITSTREAM)
        self.assertEqual("ice40", detection.family.key)
        self.assertEqual("magic", detection.method)

    def test_the_netlist_instantiates_ice40_primitives(self):
        types = set(INSTANCE_RE.findall(self.netlist))
        self.assertTrue(types, "no SB_* instance in the recorded netlist")
        self.assertIn("SB_LUT4", types)
        self.assertTrue(
            any(cell.startswith("SB_DFF") for cell in types),
            "the fixture is a counter, so the netlist must contain flip-flops: {}".format(
                sorted(types)
            ),
        )

    def test_icebox_vlog_alone_is_not_a_netlist(self):
        # Why the chain does not stop at icebox_vlog: its output is behavioural Verilog (LUT
        # equations and always blocks), which HAL's *netlist* parser cannot read. If a future
        # IceStorm emitted instances instead, the yosys step could be dropped -- and this test is
        # what would say so.
        with open(RECORDED_ICEBOX_VLOG, encoding="utf-8") as handle:
            behavioural = handle.read()
        self.assertEqual([], INSTANCE_RE.findall(behavioural))
        self.assertIn("always @(posedge", behavioural)

    def test_every_instantiated_primitive_is_in_the_shipped_gate_library(self):
        # This is the 'correct gate library reference' promise: if the chain emits a cell the
        # library does not define, HAL cannot read the netlist and the family's gate_library is
        # wrong.
        types = set(INSTANCE_RE.findall(self.netlist))
        missing = sorted(
            cell for cell in types if '"name": "{}"'.format(cell) not in self.library
        )
        self.assertEqual(
            [],
            missing,
            "ice40ultra.hgl does not define {}, which the iCE40 chain emits".format(missing),
        )

    def test_the_recorded_manifest_documents_the_real_run(self):
        with open(RECORDED_MANIFEST, encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual("ice40", manifest["detection"]["family"])
        self.assertEqual(
            ["iceunpack", "icebox_vlog", "yosys"],
            [step["program"] for step in manifest["steps"]],
        )
        self.assertTrue(manifest["gate_library"].endswith("ice40ultra.hgl"))
        # The digests of the files that are in this checkout, not of some other run's: the point
        # of a provenance manifest is that it can be checked.
        self.assertEqual(sha256_of(RECORDED_BITSTREAM), manifest["bitstream"]["sha256"])
        self.assertEqual(sha256_of(RECORDED_NETLIST), manifest["netlist"]["sha256"])
        for step in manifest["steps"]:
            self.assertTrue(step["command"], step)
            self.assertTrue(step["version"], "no version recorded for " + step["program"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
