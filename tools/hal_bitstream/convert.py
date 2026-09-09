"""Resolve the converters, run the chain, and record exactly what produced the netlist.

Two rules:

* a missing converter is an error that names the program, the project that ships it, the package
  to install and the flag to point at an existing copy -- never a silent fallback and never a
  half-converted file left behind;
* everything that went into the netlist is written next to it. Bitstream sha256, converter paths
  and versions, and the literal command lines: without that, a netlist recovered from a bitstream
  is an unciteable claim.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import __version__, registry
from .detect import detect_family

__all__ = [
    "ConversionError",
    "MissingConverterError",
    "ConversionResult",
    "parse_converter_overrides",
    "resolve_program",
    "plan",
    "convert",
]

PRODUCER = {"name": "hal_bitstream", "version": __version__}


class ConversionError(RuntimeError):
    """The conversion could not be run, or the converter failed."""


class MissingConverterError(ConversionError):
    """A converter this family needs is not installed."""


class PlannedStep(object):
    def __init__(self, step, program_path, input_path, output_path):
        self.step = step
        self.program_path = program_path
        self.input_path = input_path
        self.output_path = output_path

    @property
    def command(self):
        return self.step.command(self.program_path, self.input_path, self.output_path)


class ConversionResult(object):
    def __init__(self, detection, netlist, gate_library, manifest, steps):
        self.detection = detection
        self.netlist = netlist
        self.gate_library = gate_library
        self.manifest = manifest
        self.steps = steps


def parse_converter_overrides(entries):
    """``["icebox_vlog=/opt/bin/icebox_vlog"]`` -> ``{"icebox_vlog": "/opt/bin/icebox_vlog"}``."""
    overrides = {}
    for entry in entries or ():
        if "=" not in entry:
            raise ConversionError(
                "--converter takes NAME=PATH (e.g. --converter "
                "icebox_vlog=/opt/icestorm/bin/icebox_vlog), got {!r}".format(entry)
            )
        name, path = entry.split("=", 1)
        name = name.strip()
        path = os.path.abspath(os.path.expanduser(path.strip()))
        if not os.path.isfile(path):
            raise ConversionError("--converter {}: no such file: {}".format(name, path))
        if not os.access(path, os.X_OK):
            raise ConversionError("--converter {}: not executable: {}".format(name, path))
        overrides[name] = path
    return overrides


def resolve_program(family, step, overrides=None):
    """Absolute path of ``step``'s program, or raise :class:`MissingConverterError`."""
    overrides = overrides or {}
    if step.program in overrides:
        return overrides[step.program]
    found = shutil.which(step.program)
    if found:
        return found
    raise MissingConverterError(
        "no converter for {} bitstreams: {!r} is not on PATH.\n"
        "It is part of {}{}.\n"
        "That step would {}.\n"
        "If it is installed somewhere else, point at it with:\n"
        "    --converter {}=/path/to/{}".format(
            family.name,
            step.program,
            family.toolchain,
            " ({})".format(step.install_hint()) if step.install_hint() else "",
            step.purpose or "convert the bitstream",
            step.program,
            step.program,
        )
    )


def plan(family, input_path, output_path, overrides=None, scratch_dir=None):
    """Resolve the chain for ``family`` without running anything.

    Steps whose ``input_suffixes`` do not match are skipped, which is what lets an already
    unpacked ``.asc``/``.config`` file be converted directly.
    """
    if not family.has_converter:
        raise MissingConverterError(
            "{} has no open bitstream-to-netlist converter ({}).\n{}\n"
            "hal_bitstream detects the format and stops here on purpose: a netlist it cannot "
            "produce is not one it will invent. Register a converter in "
            "tools/hal_bitstream/registry.py when one exists{}.".format(
                family.name,
                family.toolchain,
                family.notes or "",
                " -- see {}".format(family.documentation) if family.documentation else "",
            )
        )

    scratch_dir = scratch_dir or os.path.dirname(os.path.abspath(str(output_path)))
    base = os.path.splitext(os.path.basename(str(input_path)))[0]
    planned = []
    current = os.path.abspath(str(input_path))

    # Applicability is decided against what each step would actually be handed, not against the
    # original file: a chain whose second step wants the first step's output has to keep it.
    applicable = []
    probe = current
    for step in family.steps:
        if not step.applies_to(probe):
            continue
        applicable.append(step)
        probe = "step{}{}".format(len(applicable), step.output_suffix)
    if not applicable:
        suffixes = sorted(
            {suffix for step in family.steps for suffix in (step.input_suffixes or ())}
        )
        raise ConversionError(
            "{}: {} has no step that accepts a {!r} file (its chain starts from {}). Either the "
            "file is already converted, or --family is wrong.".format(
                os.path.basename(str(input_path)),
                family.name,
                os.path.splitext(current)[1],
                ", ".join(suffixes) or "the packed bitstream",
            )
        )

    for index, step in enumerate(applicable):
        program_path = resolve_program(family, step, overrides)
        if index == len(applicable) - 1:
            step_output = os.path.abspath(str(output_path))
        else:
            step_output = os.path.join(
                scratch_dir, "{}.{}{}".format(base, index, step.output_suffix)
            )
        planned.append(PlannedStep(step, program_path, current, step_output))
        current = step_output
    return planned


#: Interpreter noise that is not a version. IceStorm's tools are Python scripts and print
#: SyntaxWarnings (plus an echo of the offending source line) before anything else, which would
#: otherwise be recorded as 'the version of icebox_vlog'.
_NOISE_MARKERS = ("Warning:", "Traceback (most recent call last)")


def _program_version(program_path, step):
    """Best-effort version string; converters disagree wildly on how to report one."""
    try:
        result = subprocess.run(
            [program_path, step.version_argument],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    skip_next = False
    for raw in (result.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            skip_next = False
            continue
        if any(marker in line for marker in _NOISE_MARKERS):
            # The line after a SyntaxWarning is the source line it complains about.
            skip_next = True
            continue
        if skip_next:
            skip_next = False
            continue
        return line[:200]
    return None


def _sha256(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(planned, timeout, log):
    for planned_step in planned:
        log("running {}".format(" ".join(planned_step.command)))
        try:
            if planned_step.step.capture_stdout:
                with open(planned_step.output_path, "wb") as handle:
                    completed = subprocess.run(
                        planned_step.command,
                        stdout=handle,
                        stderr=subprocess.PIPE,
                        timeout=timeout,
                    )
                stderr = completed.stderr.decode("utf-8", "replace")
            else:
                completed = subprocess.run(
                    planned_step.command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                )
                stderr = completed.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            raise ConversionError(
                "{} did not finish within {}s: {}".format(
                    planned_step.step.program, timeout, " ".join(planned_step.command)
                )
            )
        except OSError as error:
            raise ConversionError(
                "could not run {}: {}".format(planned_step.program_path, error)
            )
        if completed.returncode != 0:
            # A converter that fails usually leaves a truncated file behind; removing it keeps a
            # failed run from looking like a successful one to the next command.
            if planned_step.step.capture_stdout and os.path.exists(planned_step.output_path):
                os.remove(planned_step.output_path)
            raise ConversionError(
                "{} failed with exit code {}:\n    {}\n{}".format(
                    planned_step.step.program,
                    completed.returncode,
                    " ".join(planned_step.command),
                    stderr.strip(),
                )
            )
        if not os.path.exists(planned_step.output_path):
            raise ConversionError(
                "{} exited 0 but wrote no {}".format(
                    planned_step.step.program, planned_step.output_path
                )
            )
        if os.path.getsize(planned_step.output_path) == 0:
            raise ConversionError(
                "{} exited 0 but wrote an empty {}; the bitstream is probably not a {} "
                "bitstream".format(
                    planned_step.step.program,
                    os.path.basename(planned_step.output_path),
                    planned_step.step.program,
                )
            )


def convert(
    bitstream,
    output=None,
    family=None,
    overrides=None,
    gate_library=None,
    keep_intermediates=False,
    timeout=900,
    log=None,
):
    """Convert ``bitstream`` to a Verilog netlist and return a :class:`ConversionResult`."""
    log = log or (lambda message: None)
    bitstream = os.path.abspath(os.path.expanduser(str(bitstream)))
    detection = detect_family(bitstream, family)
    log(
        "{}: {} ({}, {})".format(
            os.path.basename(bitstream),
            detection.family.name,
            detection.method,
            detection.evidence,
        )
    )

    if output is None:
        output = os.path.splitext(bitstream)[0] + ".v"
    output = os.path.abspath(os.path.expanduser(str(output)))
    output_dir = os.path.dirname(output) or "."
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    scratch = None
    if not keep_intermediates:
        scratch = tempfile.mkdtemp(prefix="hal_bitstream_")
    try:
        planned = plan(
            detection.family, bitstream, output, overrides, scratch_dir=scratch or output_dir
        )
        started = time.time()
        _run(planned, timeout, log)
        elapsed = time.time() - started

        library = gate_library or detection.family.gate_library_file()
        if library and not os.path.isfile(library):
            raise ConversionError(
                "the gate library for {} is missing: {}. Pass --gate-library <file.hgl>.".format(
                    detection.family.name, library
                )
            )
        if not library:
            log(
                "warning: this fork ships no gate library for {}; pass --gate-library to load the "
                "netlist with HAL".format(detection.family.name)
            )

        manifest = {
            "producer": dict(PRODUCER),
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "bitstream": {
                "path": bitstream,
                "size_bytes": os.path.getsize(bitstream),
                "sha256": _sha256(bitstream),
            },
            "detection": detection.to_json(),
            "netlist": {
                "path": output,
                "size_bytes": os.path.getsize(output),
                "sha256": _sha256(output),
            },
            "gate_library": library,
            "duration_s": round(elapsed, 3),
            "steps": [
                {
                    "program": planned_step.step.program,
                    "resolved": planned_step.program_path,
                    "version": _program_version(planned_step.program_path, planned_step.step),
                    "command": planned_step.command,
                    "purpose": planned_step.step.purpose,
                    "input": planned_step.input_path,
                    "output": planned_step.output_path,
                }
                for planned_step in planned
            ],
        }
        return ConversionResult(detection, output, library, manifest, planned)
    finally:
        if scratch and os.path.isdir(scratch):
            shutil.rmtree(scratch, ignore_errors=True)


def write_manifest(manifest, path):
    with open(str(path), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def available_converters(family, overrides=None):
    """``[(step, path_or_None)]`` -- what ``hal_bitstream families`` prints."""
    found = []
    for step in family.steps:
        try:
            found.append((step, resolve_program(family, step, overrides)))
        except MissingConverterError:
            found.append((step, None))
    return found


if __name__ == "__main__":  # pragma: no cover - convenience only
    print("run 'python tools/hal_bitstream --help'", file=sys.stderr)
    sys.exit(2)
