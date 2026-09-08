"""Command line interface for hal_viz."""

import argparse
import os
import sys

from . import __version__
from .extract import (
    ScopeTooLarge,
    build_module_tree_graph,
    build_netlist_graph,
    collect_module_gates,
    collect_neighborhood,
)
from .halenv import (
    HalUnavailable,
    NetlistLoadError,
    find_gate,
    find_module,
    import_hal_py,
    import_plugin,
    load_all_plugins,
    load_netlist,
    unload_all_plugins,
)
from .render import (
    LAYOUT_ENGINES,
    RENDER_FORMATS,
    RenderError,
    find_dot_binary,
    render_dot,
    write_html_index,
)
from .report import ReportError, ReportOptions, load_documents, write_report

__all__ = ["main", "build_parser"]

_KNOWN_SUFFIXES = tuple("." + fmt for fmt in RENDER_FORMATS if fmt != "none") + (".dot",)


class _Reporter(object):
    """Tiny stderr logger so stdout stays free for machine-readable paths."""

    def __init__(self, quiet=False):
        self.quiet = quiet

    def info(self, message):
        if not self.quiet:
            sys.stderr.write("[hal_viz] {}\n".format(message))

    def warn(self, message):
        sys.stderr.write("[hal_viz] warning: {}\n".format(message))


def _resolve_output(output, default_stem, fmt):
    """Split ``--output`` into a base path and an effective render format.

    ``-o out/`` or an existing directory means "put ``<stem>.dot`` in here";
    ``-o graph.svg`` sets both the base name and (unless overridden) the
    format; ``-o graph`` just sets the base name.
    """
    output = str(output)
    is_dir = (
        output.endswith(("/", "\\"))
        or os.path.isdir(output)
        or os.path.basename(output) == ""
    )
    if is_dir:
        base = os.path.join(output, default_stem)
    else:
        base, suffix = os.path.splitext(output)
        if suffix.lower() in _KNOWN_SUFFIXES:
            if fmt is None and suffix.lower() != ".dot":
                fmt = suffix.lower().lstrip(".")
        else:
            base = output
    return os.path.abspath(base), (fmt or "svg")


def _ensure_parent(path):
    parent = os.path.dirname(os.path.abspath(str(path)))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return parent


def _emit(graph, base, fmt, args, reporter):
    """Write ``<base>.dot`` and, when possible, ``<base>.<fmt>``.

    Returns ``(dot_path, rendered_path_or_None)``.  A missing Graphviz binary
    is a warning, not an error: the .dot file is the primary artifact.
    """
    _ensure_parent(base)
    dot_path = base + ".dot"
    graph.write(dot_path)
    reporter.info(
        "wrote {} ({} nodes, {} edges)".format(
            dot_path, graph.node_count, graph.edge_count
        )
    )

    if fmt == "none":
        return dot_path, None

    try:
        binary = find_dot_binary(args.dot_binary)
        rendered = render_dot(
            dot_path,
            base + "." + fmt,
            fmt,
            dot_binary=binary,
            engine=args.engine,
            timeout=args.render_timeout,
        )
    except RenderError as exc:
        reporter.warn("{}\n[hal_viz] keeping {}".format(exc, dot_path))
        return dot_path, None
    reporter.info("wrote {}".format(rendered))
    return dot_path, rendered


def _maybe_write_index(args, base_dir, title, entries, reporter, notes=()):
    if not args.html:
        return None
    index_path = os.path.join(base_dir, "index.html")
    write_html_index(index_path, title, entries, notes=notes)
    reporter.info("wrote {}".format(index_path))
    return index_path


def _print_paths(paths):
    for path in paths:
        if path:
            print(path)


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_netlist_graph(args, reporter):
    hal_py = import_hal_py(args.hal_lib)
    # Every gate library and netlist format HAL can read comes from a plugin -- .hgl from
    # hgl_parser, .v from verilog_parser and so on -- and a plugin only registers its parser when it
    # is loaded. Without this, loading anything real fails with an unhelpful "no parser" error. The
    # plugins are deliberately left loaded: the process exits right after, and unloading them pulls
    # the gate library out from under the netlist we are still holding.
    load_all_plugins(hal_py)
    netlist = load_netlist(hal_py, args.netlist, args.gate_library)

    if args.module is not None and args.gate is not None:
        raise SystemExit("hal_viz: --module and --gate are mutually exclusive")

    stem = "netlist_graph"
    if args.module is not None:
        module = find_module(netlist, args.module)
        gates = collect_module_gates(module, recursive=args.recursive)
        scope = "module '{}' (id {}){}".format(
            module.get_name(), module.get_id(), ", recursive" if args.recursive else ""
        )
        stem = "module_{}_graph".format(module.get_id())
        if args.depth:
            gates = collect_neighborhood(
                gates, args.depth, args.direction, args.max_gates
            )
            scope += " + {} hop(s)".format(args.depth)
    elif args.gate is not None:
        seed = find_gate(netlist, args.gate)
        gates = collect_neighborhood(
            [seed], args.depth, args.direction, args.max_gates
        )
        scope = "gate '{}' (id {}), depth {}, {}".format(
            seed.get_name(), seed.get_id(), args.depth, args.direction
        )
        stem = "gate_{}_depth{}".format(seed.get_id(), args.depth)
    else:
        gates = netlist.get_gates()
        scope = "whole netlist"

    if len(gates) > args.max_gates:
        raise ScopeTooLarge(
            len(gates),
            args.max_gates,
            "scope the view with --module/--gate + --depth, or raise --max-gates",
        )
    if not gates:
        reporter.warn("the selected scope contains no gates; emitting an empty graph")

    design = netlist.get_design_name() or os.path.basename(str(args.netlist))
    title = "{} - {} - {} gates".format(design, scope, len(gates))
    reporter.info("building gate-level graph: {}".format(title))

    graph = build_netlist_graph(
        gates,
        title=title,
        rankdir=args.rankdir,
        net_labels=not args.no_net_labels,
        pin_labels=args.pin_labels,
        cluster_modules=args.cluster_modules,
        show_boundary=args.show_boundary,
        label_limit=args.label_limit,
        comment="generated by hal_viz {} (netlist_graph)".format(__version__),
    )

    base, fmt = _resolve_output(args.output, stem, args.format)
    dot_path, rendered = _emit(graph, base, fmt, args, reporter)
    _maybe_write_index(
        args,
        os.path.dirname(base),
        title,
        [("gate-level graph", rendered or dot_path)],
        reporter,
    )
    _print_paths([dot_path, rendered])
    return 0


def cmd_module_tree(args, reporter):
    hal_py = import_hal_py(args.hal_lib)
    load_all_plugins(hal_py)  # see cmd_netlist_graph: the parsers live in plugins
    netlist = load_netlist(hal_py, args.netlist, args.gate_library)

    root = find_module(netlist, args.module) if args.module else netlist.get_top_module()
    if root is None:
        raise NetlistLoadError("this netlist has no top module")

    reporter.info(
        "building module tree from '{}' (id {})".format(root.get_name(), root.get_id())
    )
    graph = build_module_tree_graph(
        root,
        max_depth=args.depth,
        rankdir=args.rankdir,
        label_limit=args.label_limit,
        show_gate_counts=not args.no_gate_counts,
        comment="generated by hal_viz {} (module_tree)".format(__version__),
    )

    base, fmt = _resolve_output(
        args.output, "module_tree_{}".format(root.get_id()), args.format
    )
    dot_path, rendered = _emit(graph, base, fmt, args, reporter)
    _maybe_write_index(
        args,
        os.path.dirname(base),
        "module hierarchy of {}".format(root.get_name()),
        [("module hierarchy", rendered or dot_path)],
        reporter,
    )
    _print_paths([dot_path, rendered])
    return 0


def cmd_dataflow(args, reporter):
    hal_py = import_hal_py(args.hal_lib)
    load_all_plugins(hal_py)
    try:
        dataflow = import_plugin("dataflow")
        netlist = load_netlist(hal_py, args.netlist, args.gate_library)

        out_dir = os.path.abspath(str(args.output))
        os.makedirs(out_dir, exist_ok=True)

        config = dataflow.Configuration(netlist)
        config = config.with_flip_flops()
        if args.min_group_size is not None:
            config = config.with_min_group_size(args.min_group_size)
        if args.expected_size:
            config = config.with_expected_sizes(list(args.expected_size))
        if args.stage_identification:
            config = config.with_stage_identification(True)
        if args.type_consistency:
            config = config.with_type_consistency(True)

        reporter.info("running DANA dataflow analysis (this can take a while) ...")
        result = dataflow.analyze(config)
        if result is None:
            raise RuntimeError(
                "dataflow analysis failed; see the HAL log above for details"
            )

        groups = result.get_groups()
        reporter.info("dataflow analysis recovered {} register group(s)".format(len(groups)))

        # The dataflow plugin already knows how to serialize its own result, so
        # use its writers rather than reinventing the grouping graph.
        dot_path = os.path.join(out_dir, "graph.dot")
        if not result.write_dot(out_dir):
            raise RuntimeError("dataflow result.write_dot() failed; see the HAL log")
        txt_path = os.path.join(out_dir, "groups.txt")
        if not result.write_txt(txt_path):
            reporter.warn("dataflow result.write_txt() failed; continuing")
            txt_path = None
        reporter.info("wrote {}".format(dot_path))
        if txt_path:
            reporter.info("wrote {}".format(txt_path))

        rendered = None
        if args.format != "none":
            try:
                binary = find_dot_binary(args.dot_binary)
                rendered = render_dot(
                    dot_path,
                    os.path.join(out_dir, "graph." + args.format),
                    args.format,
                    dot_binary=binary,
                    engine=args.engine,
                    timeout=args.render_timeout,
                )
                reporter.info("wrote {}".format(rendered))
            except RenderError as exc:
                reporter.warn("{}\n[hal_viz] keeping {}".format(exc, dot_path))

        entries = [("register groups", rendered or dot_path)]
        if txt_path:
            entries.append(("group listing", txt_path))
        _maybe_write_index(
            args,
            out_dir,
            "dataflow analysis of {}".format(
                netlist.get_design_name() or os.path.basename(str(args.netlist))
            ),
            entries,
            reporter,
            notes=["{} register group(s) recovered by DANA.".format(len(groups))],
        )
        _print_paths([dot_path, txt_path, rendered])
        return 0
    finally:
        unload_all_plugins(hal_py)


def cmd_clock_tree(args, reporter):
    hal_py = import_hal_py(args.hal_lib)
    load_all_plugins(hal_py)
    try:
        cte = import_plugin("clock_tree_extractor")
        netlist = load_netlist(hal_py, args.netlist, args.gate_library)

        reporter.info("extracting clock tree ...")
        tree = cte.ClockTree.from_netlist(netlist)
        if tree is None:
            raise RuntimeError(
                "clock tree extraction failed; see the HAL log above for details"
            )

        base, fmt = _resolve_output(args.output, "clock_tree", args.format)
        _ensure_parent(base)
        dot_path = base + ".dot"
        if not tree.export(dot_path):
            raise RuntimeError("ClockTree.export() failed; see the HAL log")
        reporter.info("wrote {}".format(dot_path))

        rendered = None
        if fmt != "none":
            try:
                binary = find_dot_binary(args.dot_binary)
                rendered = render_dot(
                    dot_path,
                    base + "." + fmt,
                    fmt,
                    dot_binary=binary,
                    engine=args.engine,
                    timeout=args.render_timeout,
                )
                reporter.info("wrote {}".format(rendered))
            except RenderError as exc:
                reporter.warn("{}\n[hal_viz] keeping {}".format(exc, dot_path))

        _maybe_write_index(
            args,
            os.path.dirname(base),
            "clock tree of {}".format(
                netlist.get_design_name() or os.path.basename(str(args.netlist))
            ),
            [("clock tree", rendered or dot_path)],
            reporter,
        )
        _print_paths([dot_path, rendered])
        return 0
    finally:
        unload_all_plugins(hal_py)


def _resolve_report_output(output):
    """Turn ``--output`` into the path of an .html file."""
    output = str(output)
    if output.endswith(("/", "\\")) or os.path.isdir(output) or os.path.basename(output) == "":
        return os.path.abspath(os.path.join(output, "findings_report.html"))
    if os.path.splitext(output)[1].lower() not in (".html", ".htm"):
        output += ".html"
    return os.path.abspath(output)


def cmd_report(args, reporter):
    """Render findings documents and hal_viz artifacts into one static HTML page.

    This subcommand needs no HAL and no netlist: its inputs are the JSON
    documents written against the tools/hal_findings schema plus whatever files
    they point at.  Graphviz is used only to draw a ``.dot`` that has no
    rendered sibling, and its absence is a visible marker, not an error.
    """
    documents = load_documents(args.documents)
    invalid = [document for document in documents if document.validation_errors]
    for document in invalid:
        reporter.warn(
            "{} does not validate against the findings schema ({} problem(s)); the report "
            "marks it as such".format(document.path, len(document.validation_errors))
        )

    options = ReportOptions(
        report_path=_resolve_report_output(args.output),
        title=args.title,
        embed=not args.no_embed,
        max_embed_bytes=args.max_embed_bytes,
        max_items=args.max_items,
        render_dot=args.render_dot,
        copy_evidence=args.copy_evidence,
        dot_binary=args.dot_binary,
        engine=args.engine,
        render_timeout=args.render_timeout,
        reporter=reporter,
    )
    path, builder = write_report(documents, options, args.artifact)
    reporter.info(
        "wrote {} ({} finding(s) from {} document(s), {} diagram(s) embedded, "
        "{} missing artifact(s))".format(
            path,
            sum(len(document.findings) for document in documents),
            len(documents),
            builder.embedded,
            builder.missing_evidence,
        )
    )
    _print_paths([path])
    if args.strict and invalid:
        sys.stderr.write(
            "[hal_viz] error: {} document(s) failed findings-schema validation "
            "(--strict)\n".format(len(invalid))
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def _add_common(parser, default_output):
    parser.add_argument(
        "netlist",
        help="HAL project directory, .hal file, or an HDL netlist "
        "(the latter needs --gate-library)",
    )
    parser.add_argument(
        "-g",
        "--gate-library",
        metavar="FILE",
        help="gate library (.hgl/.lib) required for HDL netlists",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=default_output,
        metavar="PATH",
        help="output file base name, or a directory when it ends with a "
        "separator (default: %(default)s)",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=RENDER_FORMATS,
        default=None,
        help="rendered output format; 'none' writes only .dot (default: svg)",
    )
    parser.add_argument(
        "--engine",
        choices=LAYOUT_ENGINES,
        default="dot",
        help="Graphviz layout engine, passed as dot -K<engine> (default: %(default)s)",
    )
    parser.add_argument(
        "--dot-binary",
        metavar="PATH",
        help="path to the Graphviz 'dot' executable (default: $HAL_VIZ_DOT or PATH)",
    )
    parser.add_argument(
        "--render-timeout",
        type=int,
        default=600,
        metavar="SECONDS",
        help="abort a Graphviz render after this long (default: %(default)s)",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="also write an index.html next to the outputs",
    )
    parser.add_argument(
        "--hal-lib",
        action="append",
        default=[],
        metavar="DIR",
        help="directory containing hal_py (repeatable; also $HAL_PY_PATH)",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="suppress progress messages"
    )
    parser.add_argument(
        "--traceback",
        action="store_true",
        help="show the full Python traceback on error",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_viz",
        description="Headless, batch visualization of HAL netlist analyses. "
        "Always writes a Graphviz .dot file and additionally renders it with "
        "the 'dot' binary when one is available.",
        epilog="Every command except 'report' requires a built HAL whose library "
        "directory is importable as hal_py (see --hal-lib / $HAL_PY_PATH); "
        "'report' only reads findings documents and files. Paths of produced "
        "files are printed to stdout, one per line.",
    )
    parser.add_argument("--version", action="version", version="hal_viz " + __version__)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # -- netlist_graph -------------------------------------------------------
    graph_parser = subparsers.add_parser(
        "netlist_graph",
        help="gate-level graph (gates as nodes, nets as edges)",
        description="Render a gate-level graph. Full netlists are usually far "
        "too large to lay out, so scope the view with --module or "
        "--gate/--depth; --max-gates guards against accidental blow-ups.",
    )
    _add_common(graph_parser, "netlist_graph")
    scope = graph_parser.add_argument_group("scope")
    scope.add_argument(
        "-m", "--module", metavar="NAME_OR_ID", help="restrict to a module's gates"
    )
    scope.add_argument(
        "--recursive",
        action="store_true",
        help="with --module, also include gates of all submodules",
    )
    scope.add_argument(
        "--gate",
        metavar="NAME_OR_ID",
        help="centre the view on a single gate and expand --depth hops",
    )
    scope.add_argument(
        "-d",
        "--depth",
        type=int,
        default=1,
        metavar="N",
        help="neighborhood radius in gates (default: %(default)s)",
    )
    scope.add_argument(
        "--direction",
        choices=("both", "successors", "predecessors"),
        default="both",
        help="direction to expand the neighborhood in (default: %(default)s)",
    )
    scope.add_argument(
        "--max-gates",
        type=int,
        default=400,
        metavar="N",
        help="refuse to render more gates than this (default: %(default)s)",
    )
    style = graph_parser.add_argument_group("style")
    style.add_argument(
        "--rankdir",
        choices=("LR", "RL", "TB", "BT"),
        default="LR",
        help="layout direction (default: %(default)s)",
    )
    style.add_argument(
        "--cluster-modules",
        action="store_true",
        help="draw a box around the gates of each module",
    )
    style.add_argument(
        "--no-net-labels", action="store_true", help="do not label edges with net names"
    )
    style.add_argument(
        "--pin-labels", action="store_true", help="label edges with source/target pins"
    )
    style.add_argument(
        "--show-boundary",
        action="store_true",
        help="draw stubs for nets crossing the scope boundary",
    )
    style.add_argument(
        "--label-limit",
        type=int,
        default=48,
        metavar="N",
        help="truncate names longer than this (default: %(default)s)",
    )
    graph_parser.set_defaults(func=cmd_netlist_graph)

    # -- module_tree ---------------------------------------------------------
    tree_parser = subparsers.add_parser(
        "module_tree",
        help="module hierarchy tree",
        description="Render the module hierarchy of a netlist as a tree.",
    )
    _add_common(tree_parser, "module_tree")
    tree_parser.add_argument(
        "-m",
        "--module",
        metavar="NAME_OR_ID",
        help="root the tree at this module (default: the top module)",
    )
    tree_parser.add_argument(
        "-d",
        "--depth",
        type=int,
        default=None,
        metavar="N",
        help="maximum number of levels below the root (default: unlimited)",
    )
    tree_parser.add_argument(
        "--rankdir",
        choices=("LR", "RL", "TB", "BT"),
        default="TB",
        help="layout direction (default: %(default)s)",
    )
    tree_parser.add_argument(
        "--no-gate-counts", action="store_true", help="omit gate counts from labels"
    )
    tree_parser.add_argument(
        "--label-limit",
        type=int,
        default=48,
        metavar="N",
        help="truncate names longer than this (default: %(default)s)",
    )
    tree_parser.set_defaults(func=cmd_module_tree)

    # -- dataflow ------------------------------------------------------------
    dataflow_parser = subparsers.add_parser(
        "dataflow",
        help="DANA dataflow analysis register groups",
        description="Run the dataflow_analysis (DANA) plugin and render the "
        "recovered register groups. Output is written into a directory.",
    )
    _add_common(dataflow_parser, "hal_viz_dataflow")
    dataflow_parser.add_argument(
        "--min-group-size",
        type=int,
        default=None,
        metavar="N",
        help="minimum register group size (DANA default: 8)",
    )
    dataflow_parser.add_argument(
        "--expected-size",
        type=int,
        action="append",
        default=[],
        metavar="N",
        help="prioritize this group size (repeatable)",
    )
    dataflow_parser.add_argument(
        "--stage-identification", action="store_true", help="enable stage identification"
    )
    dataflow_parser.add_argument(
        "--type-consistency",
        action="store_true",
        help="enforce gate type consistency within a group",
    )
    dataflow_parser.set_defaults(func=cmd_dataflow)

    # -- clock_tree ----------------------------------------------------------
    clock_parser = subparsers.add_parser(
        "clock_tree",
        help="clock tree extracted by clock_tree_extractor",
        description="Extract the clock tree with the clock_tree_extractor "
        "plugin and render its .dot export.",
    )
    _add_common(clock_parser, "clock_tree")
    clock_parser.set_defaults(func=cmd_clock_tree)

    # -- report --------------------------------------------------------------
    report_parser = subparsers.add_parser(
        "report",
        help="static HTML report from findings documents and hal_viz artifacts",
        description="Turn one or more hal_findings JSON documents (see "
        "tools/hal_findings) plus the artifacts they reference into a single "
        "self-contained HTML page: status badges, assumptions, bounds, "
        "truncation and coverage markers, relative evidence links and inlined "
        "SVG diagrams. Needs neither HAL nor a netlist; the page opens offline.",
    )
    report_parser.add_argument(
        "documents", nargs="+", metavar="DOCUMENT", help="findings JSON documents"
    )
    report_parser.add_argument(
        "-o",
        "--output",
        default="findings_report.html",
        metavar="PATH",
        help="HTML file to write, or a directory when it ends with a separator "
        "(default: %(default)s)",
    )
    report_parser.add_argument(
        "--title", default=None, metavar="TEXT", help="report title"
    )
    report_parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="PATH",
        help="an extra hal_viz output (.svg/.dot/.png) to attach to the report "
        "even though no finding references it (repeatable)",
    )
    report_parser.add_argument(
        "--no-embed",
        action="store_true",
        help="link diagrams instead of inlining them",
    )
    report_parser.add_argument(
        "--max-embed-bytes",
        type=int,
        default=4 * 1024 * 1024,
        metavar="N",
        help="refuse to inline an SVG larger than this; it is linked with a "
        "visible marker instead (default: %(default)s)",
    )
    report_parser.add_argument(
        "--max-items",
        type=int,
        default=25,
        metavar="N",
        help="how many gates/nets/assumptions/witness rows to list per finding "
        "before an explicit truncation marker (default: %(default)s)",
    )
    report_parser.add_argument(
        "--render-dot",
        choices=("auto", "always", "never"),
        default="auto",
        help="auto: render a .dot only when it has no .svg sibling; always: "
        "always re-render; never: link the .dot untouched (default: %(default)s)",
    )
    report_parser.add_argument(
        "--copy-evidence",
        action="store_true",
        help="copy referenced files next to the report so the whole directory "
        "can be moved or archived",
    )
    report_parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when a document fails findings-schema validation "
        "(the report is still written, with the failure marked)",
    )
    report_parser.add_argument(
        "--engine",
        choices=LAYOUT_ENGINES,
        default="dot",
        help="Graphviz layout engine used for .dot evidence (default: %(default)s)",
    )
    report_parser.add_argument(
        "--dot-binary",
        metavar="PATH",
        help="path to the Graphviz 'dot' executable (default: $HAL_VIZ_DOT or PATH)",
    )
    report_parser.add_argument(
        "--render-timeout",
        type=int,
        default=600,
        metavar="SECONDS",
        help="abort a Graphviz render after this long (default: %(default)s)",
    )
    report_parser.add_argument(
        "-q", "--quiet", action="store_true", help="suppress progress messages"
    )
    report_parser.add_argument(
        "--traceback",
        action="store_true",
        help="show the full Python traceback on error",
    )
    report_parser.set_defaults(func=cmd_report)

    return parser


def main(argv=None):
    """Entry point.  ``argv`` excludes the program name."""
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if getattr(args, "func", None) is None:
        parser.print_help(sys.stderr)
        return 2

    reporter = _Reporter(quiet=args.quiet)
    try:
        return args.func(args, reporter)
    except (HalUnavailable, NetlistLoadError, ScopeTooLarge, RenderError, ReportError) as exc:
        if args.traceback:
            raise
        sys.stderr.write("[hal_viz] error: {}\n".format(exc))
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        sys.stderr.write("[hal_viz] interrupted\n")
        return 130
    except Exception as exc:  # pragma: no cover - unexpected, still be helpful
        if args.traceback:
            raise
        sys.stderr.write(
            "[hal_viz] error: {}: {}\n[hal_viz] re-run with --traceback for "
            "details\n".format(type(exc).__name__, exc)
        )
        return 1
