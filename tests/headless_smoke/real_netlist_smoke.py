#!/usr/bin/env python3
"""End-to-end headless smoke test against a real, shipped example netlist.

The standalone ``tools/hal_viz`` unit tests run on any machine because they feed the graph builders
stub objects. That is deliberate, but it means they prove nothing about ``hal_py``: a binding can be
renamed, a plugin can stop building, ``ProjectManager.serialize_project`` can start writing a project
that will not reload, and all 44 of them stay green.

This script closes that gap. It needs a *built* HAL and it asserts results, not exit codes:

  1. unpack ``examples/uart.zip`` -- a real Xilinx-flavoured UART, 407 gates, 409 nets, shipping the
     ``example_library.hgl`` gate library it needs
  2. load it through ``hal_py.NetlistFactory.load_hal_project`` and check the design name, the gate
     and net counts and the full gate-type histogram
  3. run the ``graph_algorithm`` plugin over it and check the netlist graph and its (strongly)
     connected components -- the three feedback loops of sizes 68/50/15 are the UART's counters and
     state registers, so a wrong answer here is a wrong analysis, not just a crash
  4. save the netlist into a fresh project directory, reload it in-process and re-run the same
     analysis; the two results must be identical
  5. drive ``tools/hal_viz`` as a subprocess against the *reloaded* project and check the emitted
     Graphviz output: a scoped 7-gate neighbourhood and the module tree, by exact node ids

Nothing here is skipped when something is missing. A missing ``hal_py``, a missing plugin or a
missing binding is a failure with an actionable message, because a smoke test that quietly turns
itself off is worse than no smoke test at all.

Graphviz is the one exception, and only because it is an external, optional binary: without ``dot``
the ``.dot`` files are still checked and the SVG check is reported as not run. Pass
``--require-graphviz`` (as CI does) to turn that into a failure.

Run it against a build tree with::

    HAL_BASE_PATH=<build> PYTHONPATH=<build>/lib python3 tests/headless_smoke/real_netlist_smoke.py

or point it at the libraries explicitly with ``--hal-lib <build>/lib``. Add
``--work-dir <dir> --keep`` to leave the unpacked example, the round-trip project and every
generated file behind for inspection; CI does exactly that and uploads the directory when the run
fails.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ARCHIVE = REPO_ROOT / "examples" / "uart.zip"
EXAMPLE_DIR_NAME = "uart"
HAL_VIZ = REPO_ROOT / "tools" / "hal_viz"

# ---------------------------------------------------------------------------
# Everything below was read out of examples/uart.zip (uart/uart.hal, serialization format 14) and is
# a property of that archive, not of a particular HAL build. If one of these changes, either the
# example was replaced or HAL started reading it differently -- both are worth a red build.
# ---------------------------------------------------------------------------

EXPECTED_DESIGN_NAME = "test_ext_uart"
EXPECTED_GATE_COUNT = 407
EXPECTED_NET_COUNT = 409
EXPECTED_MODULE_COUNT = 1
EXPECTED_TOP_MODULE_NAME = "top_module"
EXPECTED_GATE_TYPES = {
    "FFR": 258,
    "LUT4": 73,
    "LUT6": 20,
    "LUT5": 16,
    "LUT3": 15,
    "LUT2": 14,
    "LUT1": 5,
    "BUF": 4,
    "GND": 1,
    "VCC": 1,
}

# NetlistGraph.from_netlist() creates one vertex per gate and one edge per (source, destination) pair
# of every net, keeping parallel edges -- hence 1604 rather than the 1598 distinct gate pairs.
EXPECTED_GRAPH_VERTICES = 407
EXPECTED_GRAPH_EDGES = 1604
# The design is one weakly connected block; the strongly connected components of size >= 2 are the
# receiver's clock counter, the transmitter's counter and the bit counter.
EXPECTED_WEAK_COMPONENT_SIZES = [407]
EXPECTED_STRONG_COMPONENT_SIZES = [68, 50, 15]

# A LUT6 of the receiver clock counter. Its depth-1 neighbourhood is seven gates and seven distinct
# (source, destination, net) edges -- small enough that a picture of it is actually readable.
SCOPE_SEED_GATE = "CLK_CNT_10_i_1_inst"
EXPECTED_SCOPE_NODES = {"g10", "g12", "g294", "g298", "g303", "g304", "g305"}
EXPECTED_SCOPE_EDGES = 7
# The example has a single module, so its tree is one node and no edges.
EXPECTED_MODULE_TREE_NODES = {"m1"}

_DOT_NODE_RE = re.compile(r'^\s*"([^"]+)"\s*\[')
_DOT_EDGE_RE = re.compile(r'^\s*"([^"]+)"\s*->\s*"([^"]+)"')


class SmokeError(RuntimeError):
    """A check failed, or a prerequisite for one is missing."""


def require(condition, message):
    if not condition:
        raise SmokeError(message)


def require_attr(obj, name, where):
    require(
        hasattr(obj, name),
        "{} has no '{}'. The bindings this smoke test drives are gone or were renamed; "
        "fix the test or the bindings, do not skip the check.".format(where, name),
    )
    return getattr(obj, name)


class Report(object):
    """Prints each check as it happens so a CI log shows where a run stopped."""

    def __init__(self):
        self.passed = 0

    def step(self, message):
        print("\n== {}".format(message), flush=True)

    def ok(self, message):
        self.passed += 1
        print("   ok: {}".format(message), flush=True)

    def note(self, message):
        print("   -- {}".format(message), flush=True)


# ---------------------------------------------------------------------------
# the example
# ---------------------------------------------------------------------------


def unpack_example(work_dir, report):
    report.step("unpacking {}".format(EXAMPLE_ARCHIVE.name))
    require(
        EXAMPLE_ARCHIVE.is_file(),
        "the example archive {} is missing; this test is run from a checkout, not from an "
        "installed HAL.".format(EXAMPLE_ARCHIVE),
    )
    with zipfile.ZipFile(str(EXAMPLE_ARCHIVE)) as archive:
        for name in archive.namelist():
            require(
                not os.path.isabs(name) and ".." not in Path(name).parts,
                "refusing to extract {!r} from {}".format(name, EXAMPLE_ARCHIVE),
            )
        archive.extractall(str(work_dir))

    project_dir = Path(work_dir) / EXAMPLE_DIR_NAME
    require(
        project_dir.is_dir(),
        "{} did not contain a '{}/' directory".format(EXAMPLE_ARCHIVE, EXAMPLE_DIR_NAME),
    )
    for expected in (".project.json", "uart.hal", "example_library.hgl"):
        require(
            (project_dir / expected).is_file(),
            "the unpacked example is missing {}".format(expected),
        )
    report.ok("example project at {}".format(project_dir))
    return project_dir


# ---------------------------------------------------------------------------
# HAL
# ---------------------------------------------------------------------------


def import_hal(hal_libs, report):
    report.step("importing hal_py")
    for entry in hal_libs:
        path = os.path.abspath(os.path.expanduser(entry))
        require(os.path.isdir(path), "--hal-lib directory does not exist: {}".format(entry))
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import hal_py
    except ImportError as exc:
        raise SmokeError(
            "could not import hal_py ({}).\n"
            "This test needs a built HAL. Point it at one with --hal-lib <build>/lib or\n"
            "PYTHONPATH=<build>/lib, and set HAL_BASE_PATH=<build> so the plugins and gate\n"
            "libraries are found.".format(exc)
        )
    report.ok("hal_py from {}".format(getattr(hal_py, "__file__", "<unknown>")))
    return hal_py


def load_plugins(hal_py, report):
    report.step("loading HAL plugins")
    manager = require_attr(hal_py, "plugin_manager", "hal_py")
    require_attr(manager, "load_all_plugins", "hal_py.plugin_manager")()
    try:
        import hal_plugins  # noqa: F401
    except ImportError as exc:
        raise SmokeError(
            "plugins were loaded but 'hal_plugins' is not importable ({}). Check that "
            "HAL_BASE_PATH points at the build or install tree.".format(exc)
        )
    report.ok("plugins loaded")


def import_graph_algorithm(report):
    module_name = "hal_plugins.graph_algorithm"
    try:
        module = __import__(module_name, fromlist=["graph_algorithm"])
    except ImportError as exc:
        raise SmokeError(
            "could not import {} ({}). Build HAL with -DBUILD_ALL_PLUGINS=ON (or "
            "-DPL_GRAPH_ALGORITHM=ON); this test does not skip a missing plugin.".format(
                module_name, exc
            )
        )
    require_attr(module, "NetlistGraph", module_name)
    require_attr(module, "get_connected_components", module_name)
    report.ok("{} imported".format(module_name))
    return module


def load_project(hal_py, project_dir, report):
    factory = require_attr(hal_py, "NetlistFactory", "hal_py")
    loader = require_attr(factory, "load_hal_project", "hal_py.NetlistFactory")
    netlist = loader(str(project_dir))
    require(
        netlist is not None,
        "hal_py.NetlistFactory.load_hal_project({}) returned None; see the HAL log "
        "above.".format(project_dir),
    )
    return netlist


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def check_netlist(netlist, report, label):
    report.step("checking the {} netlist".format(label))

    design = netlist.get_design_name()
    require(
        design == EXPECTED_DESIGN_NAME,
        "design name is {!r}, expected {!r}".format(design, EXPECTED_DESIGN_NAME),
    )
    report.ok("design name {!r}".format(design))

    gates = netlist.get_gates()
    require(
        len(gates) == EXPECTED_GATE_COUNT,
        "netlist has {} gates, expected {}".format(len(gates), EXPECTED_GATE_COUNT),
    )
    report.ok("{} gates".format(len(gates)))

    nets = netlist.get_nets()
    require(
        len(nets) == EXPECTED_NET_COUNT,
        "netlist has {} nets, expected {}".format(len(nets), EXPECTED_NET_COUNT),
    )
    report.ok("{} nets".format(len(nets)))

    types = Counter(gate.get_type().get_name() for gate in gates)
    require(
        dict(types) == EXPECTED_GATE_TYPES,
        "gate type histogram is {}, expected {}".format(dict(types), EXPECTED_GATE_TYPES),
    )
    report.ok("gate types {}".format(dict(sorted(types.items()))))

    modules = netlist.get_modules()
    require(
        len(modules) == EXPECTED_MODULE_COUNT,
        "netlist has {} modules, expected {}".format(len(modules), EXPECTED_MODULE_COUNT),
    )
    top = netlist.get_top_module()
    require(top is not None, "netlist has no top module")
    require(
        top.get_name() == EXPECTED_TOP_MODULE_NAME,
        "top module is {!r}, expected {!r}".format(
            top.get_name(), EXPECTED_TOP_MODULE_NAME
        ),
    )
    require(
        len(top.get_gates()) == EXPECTED_GATE_COUNT,
        "top module holds {} gates, expected {}".format(
            len(top.get_gates()), EXPECTED_GATE_COUNT
        ),
    )
    report.ok("top module {!r} with all {} gates".format(top.get_name(), EXPECTED_GATE_COUNT))


def analyze_graph(graph_algorithm, netlist, report, label):
    """Run graph_algorithm over *netlist* and return a comparable result signature."""
    report.step("running graph_algorithm on the {} netlist".format(label))

    graph = graph_algorithm.NetlistGraph.from_netlist(netlist)
    require(
        graph is not None,
        "NetlistGraph.from_netlist() returned None; see the HAL log above.",
    )

    vertices = graph.get_num_vertices()
    edges = graph.get_num_edges()
    require(
        vertices == EXPECTED_GRAPH_VERTICES,
        "netlist graph has {} vertices, expected {}".format(
            vertices, EXPECTED_GRAPH_VERTICES
        ),
    )
    require(
        edges == EXPECTED_GRAPH_EDGES,
        "netlist graph has {} edges, expected {}".format(edges, EXPECTED_GRAPH_EDGES),
    )
    report.ok("netlist graph: {} vertices, {} edges".format(vertices, edges))

    weak = graph_algorithm.get_connected_components(graph, False, 0)
    require(weak is not None, "get_connected_components(strong=False) returned None")
    covered = set()
    for component in weak:
        overlap = covered.intersection(component)
        require(not overlap, "weakly connected components overlap on vertices {}".format(sorted(overlap)[:5]))
        covered.update(component)
    require(
        covered == set(range(vertices)),
        "the weakly connected components cover {} of {} vertices; they must partition the "
        "graph".format(len(covered), vertices),
    )
    weak_sizes = sorted((len(c) for c in weak), reverse=True)
    require(
        weak_sizes == EXPECTED_WEAK_COMPONENT_SIZES,
        "weakly connected component sizes are {}, expected {}".format(
            weak_sizes, EXPECTED_WEAK_COMPONENT_SIZES
        ),
    )
    report.ok("weakly connected components partition the graph: sizes {}".format(weak_sizes))

    strong = graph_algorithm.get_connected_components(graph, True, 2)
    require(strong is not None, "get_connected_components(strong=True) returned None")
    strong_sizes = sorted((len(c) for c in strong), reverse=True)
    require(
        strong_sizes == EXPECTED_STRONG_COMPONENT_SIZES,
        "strongly connected components of size >= 2 are {}, expected {}. These are the "
        "sequential feedback loops of the design; a different answer means the analysis "
        "changed, not just the plumbing.".format(
            strong_sizes, EXPECTED_STRONG_COMPONENT_SIZES
        ),
    )
    report.ok("feedback loops (strongly connected, size >= 2): {}".format(strong_sizes))

    return {"vertices": vertices, "edges": edges, "weak": weak_sizes, "strong": strong_sizes}


def save_and_reload(hal_py, netlist, project_dir, report):
    report.step("saving and reloading the project")
    require(
        "." not in project_dir.name,
        "a project directory name must not contain a dot -- ProjectDirectory strips what it "
        "takes for an extension (got {!r})".format(project_dir.name),
    )
    # ProjectManager.create_project_directory() refuses to touch an existing directory, which would
    # make a second run against the same --work-dir fail for the wrong reason.
    if project_dir.exists():
        report.note("removing the round-trip project left by an earlier run")
        shutil.rmtree(str(project_dir))

    manager_class = require_attr(hal_py, "ProjectManager", "hal_py")
    manager = require_attr(manager_class, "instance", "hal_py.ProjectManager")()

    require(
        require_attr(manager, "create_project_directory", "ProjectManager")(str(project_dir)),
        "ProjectManager.create_project_directory({}) failed".format(project_dir),
    )
    require(
        require_attr(manager, "serialize_project", "ProjectManager")(netlist),
        "ProjectManager.serialize_project() failed",
    )

    saved_netlist_file = Path(manager.get_netlist_filename())
    require(
        saved_netlist_file.is_file() and saved_netlist_file.stat().st_size > 0,
        "the serialized netlist {} is missing or empty".format(saved_netlist_file),
    )
    project_file = project_dir / ".project.json"
    require(project_file.is_file(), "no .project.json was written to {}".format(project_dir))
    report.ok(
        "wrote {} ({} bytes) and {}".format(
            saved_netlist_file.name, saved_netlist_file.stat().st_size, project_file.name
        )
    )

    reloaded = load_project(hal_py, project_dir, report)
    report.ok("reloaded the project from {}".format(project_dir))
    return reloaded


# ---------------------------------------------------------------------------
# hal_viz
# ---------------------------------------------------------------------------


def parse_dot(path):
    """Return (nodes, edges) of a .dot file, failing on anything that is not one."""
    require(path.is_file(), "hal_viz did not write {}".format(path))
    text = path.read_text(encoding="utf-8")
    require(text.strip(), "hal_viz wrote an empty {}".format(path.name))

    body = [line for line in text.splitlines() if not line.lstrip().startswith("//")]
    require(
        body and body[0].lstrip().startswith("digraph "),
        "{} does not start with a 'digraph' header: {!r}".format(
            path.name, body[0] if body else ""
        ),
    )
    require(
        text.count("{") == text.count("}"),
        "{} has unbalanced braces ({} open, {} close)".format(
            path.name, text.count("{"), text.count("}")
        ),
    )
    require(text.rstrip().endswith("}"), "{} is truncated".format(path.name))

    nodes, edges = set(), []
    for line in body:
        edge = _DOT_EDGE_RE.match(line)
        if edge:
            edges.append((edge.group(1), edge.group(2)))
            continue
        node = _DOT_NODE_RE.match(line)
        if node:
            nodes.add(node.group(1))
    return nodes, edges


def run_hal_viz(argv, hal_libs, report):
    command = [sys.executable, str(HAL_VIZ)] + list(argv)
    for entry in hal_libs:
        command += ["--hal-lib", entry]
    report.note("$ {}".format(" ".join(command)))
    completed = subprocess.run(
        command, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise SmokeError(
            "hal_viz exited with {}\n--- stdout ---\n{}\n--- stderr ---\n{}".format(
                completed.returncode, stdout, stderr
            )
        )
    return stdout, stderr


def check_scoped_graph(project_dir, out_dir, hal_libs, report):
    report.step("rendering a scoped gate-level graph with hal_viz")
    base = out_dir / "scoped"
    stdout, _ = run_hal_viz(
        [
            "netlist_graph",
            str(project_dir),
            "--gate",
            SCOPE_SEED_GATE,
            "--depth",
            "1",
            "--format",
            "none",
            "-o",
            str(base),
        ],
        hal_libs,
        report,
    )

    dot_path = base.with_suffix(".dot")
    printed = [line.strip() for line in stdout.splitlines() if line.strip()]
    require(
        printed == [str(dot_path)],
        "hal_viz should print exactly the one .dot it produced with --format none, "
        "printed {}".format(printed),
    )

    nodes, edges = parse_dot(dot_path)
    require(
        nodes == EXPECTED_SCOPE_NODES,
        "the depth-1 neighbourhood of {} is {}, expected {}".format(
            SCOPE_SEED_GATE, sorted(nodes), sorted(EXPECTED_SCOPE_NODES)
        ),
    )
    require(
        len(edges) == EXPECTED_SCOPE_EDGES,
        "the scoped graph has {} edges, expected {}".format(
            len(edges), EXPECTED_SCOPE_EDGES
        ),
    )
    for source, target in edges:
        require(
            source in nodes and target in nodes,
            "edge {} -> {} leaves the scope but no boundary stub was requested".format(
                source, target
            ),
        )
    report.ok(
        "{}: {} nodes, {} edges, all in scope".format(
            dot_path.name, len(nodes), len(edges)
        )
    )


def check_module_tree(project_dir, out_dir, hal_libs, report):
    report.step("rendering the module tree with hal_viz")
    base = out_dir / "modules"
    run_hal_viz(
        ["module_tree", str(project_dir), "--format", "none", "-o", str(base), "-q"],
        hal_libs,
        report,
    )
    nodes, edges = parse_dot(base.with_suffix(".dot"))
    require(
        nodes == EXPECTED_MODULE_TREE_NODES,
        "the module tree has nodes {}, expected {}".format(
            sorted(nodes), sorted(EXPECTED_MODULE_TREE_NODES)
        ),
    )
    require(not edges, "the module tree of a single-module design must have no edges")
    report.ok("modules.dot: {}".format(sorted(nodes)))


def check_svg(project_dir, out_dir, hal_libs, require_graphviz, report):
    report.step("rendering the same scope to SVG")
    dot_binary = os.environ.get("HAL_VIZ_DOT") or shutil.which("dot")
    if not dot_binary:
        message = (
            "the Graphviz 'dot' binary is not installed, so the SVG render was not "
            "exercised (the .dot checks above did run)"
        )
        require(not require_graphviz, message + "; --require-graphviz was passed")
        report.note(message)
        return False

    base = out_dir / "scoped_svg"
    run_hal_viz(
        [
            "netlist_graph",
            str(project_dir),
            "--gate",
            SCOPE_SEED_GATE,
            "--depth",
            "1",
            "--format",
            "svg",
            "-o",
            str(base),
        ],
        hal_libs,
        report,
    )
    svg_path = base.with_suffix(".svg")
    require(svg_path.is_file(), "hal_viz did not write {}".format(svg_path))
    svg = svg_path.read_text(encoding="utf-8", errors="replace")
    require(len(svg) > 512, "{} is suspiciously small ({} bytes)".format(svg_path.name, len(svg)))
    require("<svg" in svg and "</svg>" in svg, "{} is not an SVG document".format(svg_path.name))
    # Graphviz names every node it lays out in a <title> element, so a graph that rendered to an
    # empty canvas is caught here rather than passing on file size alone.
    for node in sorted(EXPECTED_SCOPE_NODES):
        require(
            ">{}<".format(node) in svg,
            "{} has no <title> for the node {}".format(svg_path.name, node),
        )
    report.ok("{}: {} bytes, {} gates drawn".format(svg_path.name, len(svg), len(EXPECTED_SCOPE_NODES)))
    return True


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def run(args, report):
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    example_dir = unpack_example(work_dir, report)

    hal_py = import_hal(args.hal_lib, report)
    load_plugins(hal_py, report)
    graph_algorithm = import_graph_algorithm(report)

    # hal_viz runs in its own process, so it has to be told where hal_py is all over again. An
    # explicit --hal-lib wins; otherwise hand it the directory the module was actually imported
    # from, which is more reliable than hoping PYTHONPATH is inherited unchanged.
    hal_libs = list(args.hal_lib)
    if not hal_libs:
        hal_file = getattr(hal_py, "__file__", None)
        if hal_file:
            hal_libs = [str(Path(hal_file).resolve().parent)]

    report.step("loading the example project")
    netlist = load_project(hal_py, example_dir, report)
    report.ok("loaded {}".format(example_dir))

    check_netlist(netlist, report, "original")
    original = analyze_graph(graph_algorithm, netlist, report, "original")

    reloaded = save_and_reload(hal_py, netlist, work_dir / "roundtrip_project", report)
    check_netlist(reloaded, report, "reloaded")
    roundtrip = analyze_graph(graph_algorithm, reloaded, report, "reloaded")

    report.step("comparing the analysis before and after the round trip")
    require(
        original == roundtrip,
        "saving and reloading changed the analysis: {} became {}".format(original, roundtrip),
    )
    report.ok("identical: {}".format(original))

    out_dir = work_dir / "viz"
    out_dir.mkdir(exist_ok=True)
    saved_project = work_dir / "roundtrip_project"
    check_scoped_graph(saved_project, out_dir, hal_libs, report)
    check_module_tree(example_dir, out_dir, hal_libs, report)
    check_svg(saved_project, out_dir, hal_libs, args.require_graphviz, report)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Headless end-to-end smoke test: load examples/uart.zip through hal_py, "
        "run the graph_algorithm plugin, round-trip the project and render scoped Graphviz "
        "output with tools/hal_viz.",
    )
    parser.add_argument(
        "--hal-lib",
        action="append",
        default=[],
        metavar="DIR",
        help="directory containing hal_py (repeatable); PYTHONPATH works too",
    )
    parser.add_argument(
        "--work-dir",
        metavar="DIR",
        help="where to unpack the example and write output (default: a temporary directory)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the work directory even when the run succeeds",
    )
    parser.add_argument(
        "--require-graphviz",
        action="store_true",
        help="fail instead of reporting the SVG check as not run when 'dot' is missing",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))

    temporary = None
    if not args.work_dir:
        temporary = tempfile.mkdtemp(prefix="hal_smoke_")
        args.work_dir = temporary

    report = Report()
    try:
        run(args, report)
    except SmokeError as exc:
        print("\nFAILED: {}".format(exc), file=sys.stderr)
        print("work directory kept at {}".format(args.work_dir), file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - an unexpected failure is still a failure
        import traceback

        traceback.print_exc()
        print("\nFAILED: unexpected exception, see the traceback above", file=sys.stderr)
        print("work directory kept at {}".format(args.work_dir), file=sys.stderr)
        return 1

    print("\n{} checks passed".format(report.passed))
    if temporary and not args.keep:
        shutil.rmtree(temporary, ignore_errors=True)
    else:
        print("work directory kept at {}".format(args.work_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
