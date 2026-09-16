"""Command line for the structural crypto identification passes.

::

    python tools/hal_crypto identify    export.vo [-o findings.json]
    python tools/hal_crypto sbox        export.vo
    python tools/hal_crypto lfsr        export.vo
    python tools/hal_crypto arx         export.vo
    python tools/hal_crypto permutation export.vo
    python tools/hal_crypto ntt         export.vo
    python tools/hal_crypto fixtures    [--check | --write]
    python tools/hal_crypto elaborate   export.hal.v --gate-library FILE --hal-lib DIR

``identify`` is the aggregate: it runs every pass and emits one findings
document with the family verdict and the classical/PQC verdict.  The per-pass
subcommands emit the same document restricted to that pass, which is what you
want while debugging a recognizer.

Exit codes follow the convention the rest of this repository uses: ``0`` when
the command did what it says, ``1`` when it failed, and ``2`` with ``--strict``
when the findings contain an error or an unsupported result.  ``unknown`` is
deliberately *not* blocking: ``none-detected`` is a real answer, not a failure.
"""

import argparse
import os
import sys

from hal_agilex import vo_netlist
from hal_agilex.primitives import UnsupportedConfiguration

from hal_findings import serialize, validate

from . import arx, classify, findings, ntt, permutation, sbox, shiftreg
from .fixtures import synth
from .netlist_model import ConeTooWide, NetlistModel, UnsupportedCell

BLOCKING_STATUSES = ("counterexample", "bounded_counterexample", "error", "unsupported")

FIXTURE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _emit(document, output, strict):
    validate.validate_document(document)
    text = serialize.dumps(document)
    if output:
        with open(output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        print(output)
    else:
        sys.stdout.write(text)
    if strict:
        for finding in document["findings"]:
            if finding["status"] in BLOCKING_STATUSES:
                print(
                    "strict: {} is {}".format(finding["id"], finding["status"]),
                    file=sys.stderr,
                )
                return 2
    return 0


def _load(args):
    netlist = vo_netlist.parse_file(args.export)
    artifact = findings.artifact_for(netlist, args.export, args.artifact_id)
    return netlist, artifact


def _single_pass_document(artifact, items, entry_point):
    return findings.document(
        {"name": entry_point, "version": "1.0.0"}, artifact, items, entry_point
    )


def command_identify(args):
    netlist, artifact = _load(args)
    document = classify.build_document(netlist, artifact)
    return _emit(document, args.output, args.strict)


def command_sbox(args):
    netlist, artifact = _load(args)
    model = NetlistModel(netlist)
    items = classify.sbox_findings(artifact["artifact_id"], sbox.identify(model))
    return _emit(
        _single_pass_document(artifact, items, "hal_crypto.sbox.identify"),
        args.output,
        args.strict,
    )


def command_lfsr(args):
    netlist, artifact = _load(args)
    model = NetlistModel(netlist)
    items = classify.shift_findings(
        artifact["artifact_id"], shiftreg.find_shift_structures(model)
    )
    return _emit(
        _single_pass_document(artifact, items, "hal_crypto.shiftreg.find_shift_structures"),
        args.output,
        args.strict,
    )


def command_arx(args):
    netlist, artifact = _load(args)
    model = NetlistModel(netlist)
    items = classify.arx_findings(artifact["artifact_id"], arx.identify(model))
    return _emit(
        _single_pass_document(artifact, items, "hal_crypto.arx.identify"),
        args.output,
        args.strict,
    )


def command_permutation(args):
    netlist, artifact = _load(args)
    model = NetlistModel(netlist)
    wiring = permutation.find_permutations(model)
    items = classify.permutation_findings(
        artifact["artifact_id"],
        wiring,
        cone_maps=permutation.cone_support_maps(model, wiring=wiring),
    )
    return _emit(
        _single_pass_document(
            artifact, items, "hal_crypto.permutation.find_permutations"
        ),
        args.output,
        args.strict,
    )


def command_ntt(args):
    netlist, artifact = _load(args)
    model = NetlistModel(netlist)
    items = classify.ntt_findings(artifact["artifact_id"], ntt.identify(model))
    return _emit(
        _single_pass_document(artifact, items, "hal_crypto.ntt.identify"),
        args.output,
        args.strict,
    )


def command_fixtures(args):
    if args.write:
        for path in synth.write_all(FIXTURE_ROOT):
            print(path)
        return 0
    stale = synth.check_all(FIXTURE_ROOT)
    if stale:
        print(
            "these fixtures differ from what fixtures/synth.py generates; run "
            "'python tools/hal_crypto fixtures --write':",
            file=sys.stderr,
        )
        for path in stale:
            print("  {}".format(path), file=sys.stderr)
        return 1
    print("{}: {} fixtures up to date".format(FIXTURE_ROOT, len(synth.FIXTURES)))
    return 0


def command_elaborate(args):
    from . import hal_adapter

    hal_py = hal_adapter.import_hal(args.hal_lib)
    netlist = hal_adapter.load_netlist(hal_py, args.netlist, args.gate_library)
    document = hal_adapter.build_document(
        hal_py, netlist, args.netlist, artifact_id=args.artifact_id
    )
    return _emit(document, args.output, args.strict)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_crypto",
        description=(
            "Structural crypto identification for Agilex 3 netlists: S-box "
            "extraction and matching, LFSR/NLFSR feedback polynomials, ARX rounds, "
            "permutation layers, NTT butterflies, and a family + classical/PQC "
            "classifier over all of them."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 2 when the findings contain an error or unsupported result",
    )
    subparsers = parser.add_subparsers(dest="command")

    def add_common(subparser):
        subparser.add_argument("export")
        subparser.add_argument("-o", "--output", help="write the findings here")
        subparser.add_argument("--artifact-id", help="artifact id used in the findings")

    for name, function, help_text in (
        ("identify", command_identify,
         "run every pass and emit the family + classical/PQC verdict"),
        ("sbox", command_sbox, "extract S-boxes and match them against the library"),
        ("lfsr", command_lfsr,
         "classify shift chains: LFSR (with polynomial), NLFSR (with ANF), or open"),
        ("arx", command_arx, "look for add-rotate-XOR round structure"),
        ("permutation", command_permutation,
         "find pure-wire bit permutations and match known pLayers/rotations"),
        ("ntt", command_ntt,
         "find modular-arithmetic butterflies and read the modulus candidates"),
    ):
        subparser = subparsers.add_parser(name, help=help_text)
        add_common(subparser)
        subparser.set_defaults(func=function)

    fixtures_parser = subparsers.add_parser(
        "fixtures", help="regenerate (or check) the synthesized fixture netlists"
    )
    group = fixtures_parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", default=True,
                       help="fail if a committed fixture is out of date (default)")
    group.add_argument("--write", action="store_true",
                       help="rewrite the committed fixtures from synth.py")
    fixtures_parser.set_defaults(func=command_fixtures)

    elaborate_parser = subparsers.add_parser(
        "elaborate",
        help="run the passes against a netlist loaded through hal_py (needs a build)",
    )
    elaborate_parser.add_argument("netlist")
    elaborate_parser.add_argument("--gate-library", required=True)
    elaborate_parser.add_argument("--hal-lib", action="append", default=[],
                                  help="directory containing hal_py (repeatable)")
    elaborate_parser.add_argument("-o", "--output")
    elaborate_parser.add_argument("--artifact-id")
    elaborate_parser.set_defaults(func=command_elaborate)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except (
        vo_netlist.VerilogSubsetError,
        UnsupportedConfiguration,
        UnsupportedCell,
        ConeTooWide,
    ) as exc:
        print("{}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print("{}".format(exc), file=sys.stderr)
        return 1
