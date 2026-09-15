"""Join a ``dag`` drawing to a ``hal_agilex trace`` and emit one HTML page.

``hal_viz dag`` draws the structure; ``tools/hal_agilex trace`` records the
values.  This module is the join, and it is deliberately the *only* place that
knows how the two line up:

*   Graphviz already writes a ``<title>`` into every ``<g class="node">`` and
    ``<g class="edge">`` of an SVG, and that title is the DOT identifier --
    ``g12``, ``tie0_7``, ``g4->g10``.  The drawing therefore needs no extra
    ids, classes or attributes to be joinable, and the ``dag`` emitter is left
    exactly as it is.
*   The emitted ``.dot`` carries each node's label, whose first line is the
    gate name.  Gate names survive ``hal_agilex import`` unchanged, so they are
    the key into the trace.  For a walkthrough that analyses an *anonymised*
    netlist the drawing's names are the anonymised ones, and ``--name-map``
    supplies the committed real -> anonymised mapping.

What comes out is one file: inline SVG, inline CSS, inline JS, inline trace.
No request leaves the page.

Nothing is guessed.  A node whose gate is not in the trace, an edge whose two
endpoints share no net (or share more than one, so which net the drawn edge
stands for is ambiguous), and a net the simulator could not resolve all render
as *unknown* and are counted in the page's own summary.
"""

import html
import json
import os
import re

__all__ = [
    "ClockStepError",
    "LEGEND_ROWS",
    "parse_dag_dot",
    "bind_trace",
    "build_page",
    "write_page",
]


class ClockStepError(Exception):
    """The drawing and the trace cannot be joined."""


_NODE_RE = re.compile(r'^\s*"([^"]+)"\s*\[(.*)\];\s*$')
_EDGE_RE = re.compile(r'^\s*"([^"]+)"\s*->\s*"([^"]+)"\s*(?:\[(.*)\])?;\s*$')
_LABEL_RE = re.compile(r'label="((?:[^"\\]|\\.)*)"')
_TIE_RE = re.compile(r"^tie([01])_\d+$")

#: Nodes the drawing adds for the reader, not for the circuit.
_DECORATION_PREFIXES = ("lvl_", "legend")

#: The value vocabulary of the page.  Kept next to the drawing's own
#: :data:`hal_viz.extract.LEGEND_ROWS` vocabulary rather than replacing it: the
#: shapes still mean what ``dag`` says they mean, colour is the new axis.
LEGEND_ROWS = (
    ("green, thick", "the net carries 1 in this cycle", "one"),
    ("grey, thin", "the net carries 0 in this cycle", "zero"),
    (
        "amber, dotted",
        "unknown: outside the trace, or a net the simulator could not resolve "
        "(an undriven net, an x literal). Never a guessed 0",
        "unk",
    ),
    (
        "0 / 1 next to a gate",
        "the value on a flip-flop output or a primary output this cycle; "
        "primary outputs are labelled with the port name",
        None,
    ),
    (
        "shape and dash pattern",
        "unchanged from the static drawing: rounded box = combinational gate, "
        "square box = flip-flop, octagon = gate on a primary I/O net, small "
        "circle = constant tie-off, dashed arrow = edge cut at a register",
        None,
    ),
)


# ---------------------------------------------------------------------------
# the drawing
# ---------------------------------------------------------------------------


def parse_dag_dot(text):
    """Read a ``hal_viz dag`` .dot into ``(nodes, edges)``.

    ``nodes`` maps the DOT node id to ``{"name", "type", "tie"}`` -- the gate
    name and type come from the three-line label the ``dag`` emitter writes,
    ``tie`` is 0/1 for a constant stub and ``None`` otherwise.  ``edges`` is the
    ordered list of ``(source, target)`` pairs.  Legend and level-marker nodes
    are dropped: they decode the picture, they are not in it.
    """
    nodes = {}
    edges = []
    for line in text.splitlines():
        edge = _EDGE_RE.match(line)
        if edge:
            source, target = edge.group(1), edge.group(2)
            if _is_decoration(source) or _is_decoration(target):
                continue
            edges.append((source, target))
            continue
        node = _NODE_RE.match(line)
        if not node:
            continue
        node_id = node.group(1)
        if _is_decoration(node_id):
            continue
        tie = _TIE_RE.match(node_id)
        label = _LABEL_RE.search(node.group(2))
        pieces = label.group(1).split("\\n") if label else []
        entry = {"name": None, "type": None, "tie": int(tie.group(1)) if tie else None}
        if tie is None and pieces:
            entry["name"] = pieces[0]
            if len(pieces) > 1:
                entry["type"] = pieces[1].strip("[]")
        nodes[node_id] = entry
    if not nodes:
        raise ClockStepError(
            "no gate or tie-off nodes found in the .dot; is this a hal_viz dag drawing?"
        )
    return nodes, edges


def _is_decoration(node_id):
    return node_id.startswith(_DECORATION_PREFIXES)


#: ``name$b3`` is how the walkthrough anonymisers spell ``name[3]`` after they
#: split a bus into unrelated scalars (see
#: ``examples/agilex3_walkthroughs/02_traffic_fsm/anonymize.py``), so a map they
#: wrote can be keyed by the split spelling while the export still says
#: ``name[3]``.  Registering the bracket form as an alias is the exact inverse
#: of that one rewrite -- not a fuzzy match.
_BUS_SPLIT_RE = re.compile(r"^(.*)\$b(\d+)$")


def load_name_map(path):
    """Read a committed anonymisation map as ``{trace name: drawing name}``.

    Accepts either a flat ``{"real": "anon"}`` object or the richer document
    the walkthrough anonymisers write, whose ``"gates"`` member has that shape.
    """
    with open(str(path), encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict) and isinstance(data.get("gates"), dict):
        data = data["gates"]
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        raise ClockStepError(
            "{}: expected an object mapping name to name, or one with a "
            '"gates" member of that shape'.format(path)
        )
    mapping = dict(data)
    for key, value in data.items():
        split = _BUS_SPLIT_RE.match(key)
        if split:
            mapping.setdefault("{}[{}]".format(split.group(1), split.group(2)), value)
    return mapping


# ---------------------------------------------------------------------------
# the join
# ---------------------------------------------------------------------------


def bind_trace(nodes, edges, trace, name_map=None):
    """Resolve every drawn node and edge to a net index in *trace*.

    Returns ``(binding, report)``.  ``binding`` is what the page's script
    consumes::

        {"nodes": {"g1": {"net": 20, "seq": true, "ports": ["btn_state"]},
                   "tie0_3": {"const": 0},
                   "g9": {"unknown": true}},
         "edges": {"g1->g3": {"net": 20}, "tie0_3->g1": {"const": 0}}}

    A node or edge of the *circuit* that cannot be resolved is present with
    ``{"unknown": true}``, so the page draws it as unknown.  Being **absent** is
    a third thing entirely: it means the element is not part of the circuit --
    the legend cluster and the level markers -- and the page leaves it alone,
    because recolouring the legend's own sample arrow by a value it does not
    have would corrupt the key the reader is using.  ``report`` counts what
    happened so the page (and the caller's log) can state it.
    """
    gates = trace.get("gates") or {}
    if not gates:
        raise ClockStepError("the trace document lists no gates")
    rename = dict(name_map or {})
    # the drawing's name -> the trace's name
    drawn_name = {rename.get(name, name): name for name in gates}

    binding = {"nodes": {}, "edges": {}}
    report = {
        "nodes": len(nodes),
        "nodes_bound": 0,
        "nodes_unknown": [],
        "ties": 0,
        "edges": len(edges),
        "edges_bound": 0,
        "edges_ambiguous": 0,
        "edges_unknown": 0,
    }

    outputs_of = {}
    inputs_of = {}
    for node_id, entry in sorted(nodes.items()):
        if entry["tie"] is not None:
            binding["nodes"][node_id] = {"const": entry["tie"]}
            report["ties"] += 1
            continue
        gate_name = drawn_name.get(entry["name"])
        gate = gates.get(gate_name) if gate_name else None
        if gate is None:
            report["nodes_unknown"].append(entry["name"])
            binding["nodes"][node_id] = {"unknown": True}
            continue
        outputs_of[node_id] = set(gate.get("outputs", {}).values())
        inputs_of[node_id] = set(gate.get("inputs", {}).values())
        record = {"gate": gate_name}
        if "output" in gate:
            record["net"] = gate["output"]
        if gate.get("sequential"):
            record["seq"] = True
        if gate.get("drives_ports"):
            record["ports"] = list(gate["drives_ports"])
        binding["nodes"][node_id] = record
        report["nodes_bound"] += 1
    report["nodes_unknown"] = sorted(set(report["nodes_unknown"]))

    # Graphviz titles an arrow "source->target", so two arrows between the same
    # pair are indistinguishable in the SVG: they are one key here.
    seen = set()
    for source, target in edges:
        key = "{}->{}".format(source, target)
        if key in seen:
            continue
        seen.add(key)
        tie = binding["nodes"].get(source, {}).get("const")
        if tie is not None:
            binding["edges"][key] = {"const": tie}
            report["edges_bound"] += 1
            continue
        shared = outputs_of.get(source, set()) & inputs_of.get(target, set())
        if len(shared) == 1:
            binding["edges"][key] = {"net": shared.pop()}
            report["edges_bound"] += 1
            continue
        if len(shared) > 1:
            # Two nets run from this driver to this sink; the drawing has one
            # arrow per net but Graphviz gives both the same title, so which
            # one this arrow is cannot be recovered.  Say unknown.
            report["edges_ambiguous"] += 1
        else:
            report["edges_unknown"] += 1
        binding["edges"][key] = {"unknown": True}
    report["edges"] = len(seen)
    return binding, report


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


_CSS = """\
:root{color-scheme:light;--one:#0b8457;--zero:#8894a0;--unk:#b7791f}
*{box-sizing:border-box}
body{font:14px/1.55 system-ui,-apple-system,Segoe UI,Helvetica,sans-serif;
margin:0;padding:1.5rem;color:#1b1b1b;background:#fff}
main{max-width:100%;margin:0 auto}
h1{font-size:1.35rem;margin:0 0 .2rem}
p.caption{color:#444;margin:.2rem 0 .8rem}
ul.stats{list-style:none;display:flex;flex-wrap:wrap;gap:.4rem;padding:0;margin:0 0 .8rem}
ul.stats li{border:1px solid #e0e0e0;border-radius:999px;padding:.1rem .7rem;
background:#fafafa;color:#333;font-size:.85rem}
ul.stats li b{font-weight:600}
p.warning{border-left:4px solid #c0392b;background:#fdecea;margin:0 0 1rem;
padding:.5rem .75rem;color:#7d241a}
.bounds{border-left:4px solid #2a5d8f;background:#f2f7fc;margin:0 0 1rem;
padding:.55rem .8rem;color:#22405c}
.bounds dl{display:grid;grid-template-columns:max-content 1fr;gap:.15rem .9rem;margin:0}
.bounds dt{font-weight:600}
.bounds dd{margin:0}
.controls{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;
align-items:center;gap:.5rem;padding:.5rem .6rem;margin:0 0 .6rem;
border:1px solid #ddd;border-radius:6px;background:#fbfbfb}
.controls button{font:inherit;padding:.2rem .7rem;border:1px solid #bbb;
border-radius:4px;background:#fff;cursor:pointer}
.controls button:hover{background:#f0f4f8}
.controls input[type=range]{flex:1 1 12rem;min-width:8rem}
.controls .counter{font-variant-numeric:tabular-nums;font-weight:600;min-width:9rem}
.controls .hint{color:#777;font-size:.8rem}
.frame-state{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
font-size:.85rem;color:#333;background:#f6f8fa;border:1px solid #e3e6e8;
border-radius:6px;padding:.4rem .6rem;margin:0 0 .6rem;overflow-x:auto;
white-space:nowrap}
.frame-state b{color:#0b8457}
.figure{border:1px solid #ddd;border-radius:6px;background:#fff;padding:.5rem;
overflow:auto;max-height:78vh;resize:vertical}
.figure svg{height:auto;display:block}
text.csv{font:bold 11px ui-monospace,SFMono-Regular,Consolas,monospace}
.legend{margin:1.25rem 0 0;border:1px solid #e0e0e0;border-radius:6px;
padding:.75rem 1rem;background:#fcfcfc}
.legend h2{font-size:.95rem;margin:0 0 .5rem}
.legend dl{display:grid;grid-template-columns:max-content 1fr;gap:.3rem .9rem;margin:0}
.legend dt{font-weight:600;color:#2a5d8f;white-space:nowrap}
.legend dd{margin:0;color:#444}
.legend i.sw{display:inline-block;width:1.6rem;height:.32rem;border-radius:2px;
margin-right:.45rem;vertical-align:middle}
.legend i.sw-one{background:var(--one);height:.45rem}
.legend i.sw-zero{background:var(--zero)}
.legend i.sw-unk{background:repeating-linear-gradient(90deg,var(--unk) 0 2px,
transparent 2px 6px)}
footer{margin-top:1.5rem;color:#777;font-size:.8rem}
"""

_JS = """\
(function () {
  "use strict";
  var T = window.__HAL_CLOCK_STEP__;
  var svg = document.querySelector(".figure svg");
  if (!svg) { return; }

  var ONE = "#0b8457", ZERO = "#8894a0", UNK = "#b7791f";
  var FILL = {"1": "#d9f0e4", "0": "#f0f2f4", "x": "#fdf1dc"};

  function titleOf(group) {
    var node = group.firstElementChild;
    while (node) {
      if (node.tagName === "title") { return node.textContent.trim(); }
      node = node.nextElementSibling;
    }
    return null;
  }

  /* Collect the drawn elements once; per-cycle work is then a flat loop. */
  var nodes = [], edges = [];
  Array.prototype.forEach.call(svg.querySelectorAll("g.node"), function (group) {
    var id = titleOf(group);
    var bound = id ? T.binding.nodes[id] : null;
    if (!bound) { return; }
    var shapes = group.querySelectorAll("polygon, ellipse, path");
    var label = null;
    if (bound.seq || bound.ports) {
      var box = null;
      try { box = group.getBBox(); } catch (err) { box = null; }
      if (box) {
        label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("class", "csv");
        label.setAttribute("x", box.x + box.width + 5);
        label.setAttribute("y", box.y + box.height / 2 + 4);
        label.setAttribute("text-anchor", "start");
        group.appendChild(label);
      }
    }
    nodes.push({bound: bound, shapes: shapes, label: label});
  });
  Array.prototype.forEach.call(svg.querySelectorAll("g.edge"), function (group) {
    var id = titleOf(group);
    var bound = id ? T.binding.edges[id] : null;
    /* Absent means "not part of the circuit": the legend's own sample arrows
       live in the same SVG, and painting a value onto the key would break the
       key. An unresolvable circuit edge is present as {unknown: true}. */
    if (!bound) { return; }
    var paths = group.querySelectorAll("path");
    /* The dash pattern is the drawing's own vocabulary -- dashed means "cut at
       a register" -- so remember it and put it back; only unknown overrides
       it, and only for as long as the value is unknown. */
    var dashes = [];
    for (var k = 0; k < paths.length; k++) {
      dashes.push(paths[k].getAttribute("stroke-dasharray") || "none");
    }
    edges.push({
      bound: bound,
      paths: paths,
      dashes: dashes,
      heads: group.querySelectorAll("polygon")
    });
  });

  function valueOf(bound, frame) {
    if (!bound) { return "x"; }
    if (bound.const === 0 || bound.const === 1) { return String(bound.const); }
    if (typeof bound.net !== "number") { return "x"; }
    var character = frame.values.charAt(bound.net);
    return (character === "0" || character === "1") ? character : "x";
  }

  function colourOf(value) {
    return value === "1" ? ONE : (value === "0" ? ZERO : UNK);
  }

  function render(index) {
    var frame = T.frames[index];
    var i, j, value, colour, shape;
    for (i = 0; i < nodes.length; i++) {
      value = valueOf(nodes[i].bound, frame);
      colour = colourOf(value);
      for (j = 0; j < nodes[i].shapes.length; j++) {
        shape = nodes[i].shapes[j];
        shape.setAttribute("stroke", colour);
        shape.setAttribute("stroke-width", value === "1" ? "2.4" : "1.4");
        shape.setAttribute("stroke-dasharray", value === "x" ? "2,2" : "none");
        if (shape.getAttribute("fill") !== "none") {
          shape.setAttribute("fill", FILL[value]);
        }
      }
      if (nodes[i].label) {
        var ports = nodes[i].bound.ports;
        nodes[i].label.setAttribute("fill", colour);
        nodes[i].label.textContent = ports ? ports.join(",") + "=" + value : value;
      }
    }
    for (i = 0; i < edges.length; i++) {
      value = valueOf(edges[i].bound, frame);
      colour = colourOf(value);
      for (j = 0; j < edges[i].paths.length; j++) {
        edges[i].paths[j].setAttribute("stroke", colour);
        edges[i].paths[j].setAttribute("stroke-width", value === "1" ? "2.2" : "1");
        edges[i].paths[j].setAttribute(
          "stroke-dasharray", value === "x" ? "1,3" : edges[i].dashes[j]
        );
      }
      for (j = 0; j < edges[i].heads.length; j++) {
        edges[i].heads[j].setAttribute("stroke", colour);
        edges[i].heads[j].setAttribute("fill", colour);
      }
    }
    document.getElementById("counter").textContent =
      "cycle " + frame.cycle + "  (" + (index + 1) + " of " + T.frames.length + ")";
    document.getElementById("scrub").value = String(index);
    document.getElementById("frame-state").innerHTML = describe(frame);
  }

  function escapeText(text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;");
  }

  function describe(frame) {
    var parts = [], name;
    parts.push("in:");
    for (name in frame.inputs) {
      if (Object.prototype.hasOwnProperty.call(frame.inputs, name)) {
        parts.push(escapeText(name) + "=<b>" + escapeText(frame.inputs[name]) + "</b>");
      }
    }
    parts.push("&nbsp; out:");
    for (name in frame.outputs) {
      if (Object.prototype.hasOwnProperty.call(frame.outputs, name)) {
        var value = frame.outputs[name];
        parts.push(escapeText(name) + "=<b>" +
                   (value === null ? "unknown" : escapeText(value)) + "</b>");
      }
    }
    return parts.join(" ");
  }

  var index = 0, timer = null;

  function go(next) {
    index = (next + T.frames.length) % T.frames.length;
    render(index);
  }

  function stop() {
    if (timer !== null) { window.clearInterval(timer); timer = null; }
    document.getElementById("play").textContent = "\\u25b6 play";
  }

  function toggle() {
    if (timer !== null) { stop(); return; }
    timer = window.setInterval(function () { go(index + 1); }, 700);
    document.getElementById("play").textContent = "\\u23f8 pause";
  }

  document.getElementById("prev").addEventListener("click", function () {
    stop(); go(index - 1);
  });
  document.getElementById("next").addEventListener("click", function () {
    stop(); go(index + 1);
  });
  document.getElementById("play").addEventListener("click", toggle);
  document.getElementById("scrub").addEventListener("input", function (event) {
    stop(); go(parseInt(event.target.value, 10) || 0);
  });
  /* Graphviz sizes the drawing in points; 100% zoom is that natural size, so
     the picture starts at the size the static dag.svg has and the reader can
     shrink a tall graph to see its shape or grow it to read a label. */
  var declared = svg.getAttribute("width") || "";
  var natural = parseFloat(declared) || 0;
  if (/pt\\s*$/.test(declared)) { natural = natural * 96 / 72; }

  function setZoom(percent) {
    if (!natural) { return; }
    svg.style.width = (natural * percent / 100) + "px";
    svg.style.height = "auto";
  }

  document.getElementById("zoom").addEventListener("input", function (event) {
    setZoom(parseInt(event.target.value, 10) || 100);
  });
  document.addEventListener("keydown", function (event) {
    if (event.target && /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") { return; }
    }
    if (event.key === "ArrowLeft") { stop(); go(index - 1); event.preventDefault(); }
    else if (event.key === "ArrowRight") { stop(); go(index + 1); event.preventDefault(); }
    else if (event.key === " ") { toggle(); event.preventDefault(); }
  });

  document.getElementById("scrub").max = String(T.frames.length - 1);
  setZoom(100);
  go(0);
}());
"""


def _embed_json(payload):
    """JSON safe to drop into an inline ``<script>`` element."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace(
        "\u2029", "\\u2029"
    )


def _stimulus_rows(trace):
    stimulus = trace.get("stimulus", {})
    window = trace.get("window", {})
    driveable = stimulus.get("driveable_inputs") or []
    rows = [
        (
            "cycle bound",
            "cycles {}-{} of one run: {} recorded cycle(s). Nothing outside that "
            "window is shown, and nothing about it is claimed.".format(
                window.get("start", 0),
                window.get("end", 0),
                window.get("cycles", len(trace.get("frames", ()))),
            ),
        ),
        (
            "stimulus",
            "{} driven from random.Random({}), one draw per input per cycle{}".format(
                ", ".join(
                    "{}[{}]".format(name, width) if width > 1 else name
                    for name, width in driveable
                )
                or "no driveable input",
                stimulus.get("seed"),
                "; " + ", ".join(
                    "{} held at {}".format(name, value)
                    for name, value in sorted((stimulus.get("held_inputs") or {}).items())
                )
                if stimulus.get("held_inputs")
                else "",
            ),
        ),
    ]
    if stimulus.get("clear_input"):
        rows.append(
            (
                "asynchronous clear",
                "{} asserted before the first cycle and on cycle(s) {}".format(
                    stimulus["clear_input"],
                    ", ".join(str(cycle) for cycle in stimulus.get("clear_cycles", []))
                    or "none",
                ),
            )
        )
    if stimulus.get("ignored_inputs"):
        rows.append(
            (
                "not driven",
                "{} -- the export has no clock net; the simulator steps the "
                "registers itself".format(", ".join(stimulus["ignored_inputs"])),
            )
        )
    return rows


#: Shown when the drawing is of an anonymised netlist -- which is exactly the
#: case in which a ``--name-map`` was needed to join it to the trace.
BLINDED_NOTE = (
    "Spoiler warning. This drawing is of the walkthrough's anonymised netlist, "
    "so its gate names are the blinded ones; the values come from a trace of "
    "the named export of the same circuit, so the port and signal names in the "
    "stimulus and the per-cycle row below are the design's real ones. If you "
    "are working the exercise, come back to this page after the un-blinding "
    "step."
)


def build_page(
    svg_markup,
    trace,
    binding,
    report,
    title,
    caption=None,
    footer=None,
    blinded=False,
):
    """Return the complete HTML text of the clock-step page."""
    frames = [
        {
            "cycle": frame["cycle"],
            "inputs": frame["inputs"],
            "outputs": frame["outputs"],
            "values": frame["values"],
        }
        for frame in trace.get("frames", ())
    ]
    if not frames:
        raise ClockStepError("the trace document has no frames")

    unknown_nodes = report["nodes"] - report["nodes_bound"] - report["ties"]
    unknown_edges = report["edges"] - report["edges_bound"]
    warnings = []
    if blinded:
        warnings.append(BLINDED_NOTE)
    if unknown_nodes:
        warnings.append(
            "{} drawn gate(s) are not in the trace and stay marked unknown: "
            "{}".format(unknown_nodes, ", ".join(report["nodes_unknown"][:8]))
        )
    if report["edges_ambiguous"]:
        warnings.append(
            "{} drawn edge(s) join a driver and a sink that share more than one "
            "net; which net the arrow stands for cannot be recovered from the "
            "drawing, so they stay unknown".format(report["edges_ambiguous"])
        )

    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>{}</title>".format(html.escape(title)),
        "<style>",
        _CSS.rstrip(),
        "</style></head><body><main>",
        "<h1>{}</h1>".format(html.escape(title)),
    ]
    if caption:
        parts.append('<p class="caption">{}</p>'.format(html.escape(str(caption))))
    parts.append('<ul class="stats">')
    for label, value in (
        ("recorded cycles", len(frames)),
        ("gates with a value", report["nodes_bound"]),
        ("tie-offs", report["ties"]),
        ("edges with a value", report["edges_bound"]),
        ("edges unknown", unknown_edges),
        ("nets in the trace", len(trace.get("nets", ()))),
    ):
        parts.append(
            "<li><b>{}</b> {}</li>".format(html.escape(str(value)), html.escape(label))
        )
    parts.append("</ul>")
    for warning in warnings:
        parts.append('<p class="warning">{}</p>'.format(html.escape(warning)))

    parts.append('<section class="bounds"><dl>')
    for term, meaning in _stimulus_rows(trace):
        parts.append(
            "<dt>{}</dt><dd>{}</dd>".format(html.escape(term), html.escape(meaning))
        )
    parts.append(
        "<dt>semantics</dt><dd>{}</dd>".format(
            html.escape(
                "Values come from tools/hal_agilex trace, which simulates the "
                "vendor export with the modelled tennm_lcell_comb and tennm_ff "
                "semantics only, two-valued, without timing. A net the "
                "simulator cannot resolve is drawn unknown, never as 0."
            )
        )
    )
    parts.append("</dl></section>")

    parts.append('<div class="controls">')
    parts.append('<button id="prev" type="button">&#9664; prev</button>')
    parts.append('<button id="play" type="button">&#9654; play</button>')
    parts.append('<button id="next" type="button">next &#9654;</button>')
    parts.append(
        '<input id="scrub" type="range" min="0" max="{}" step="1" value="0" '
        'aria-label="cycle">'.format(len(frames) - 1)
    )
    parts.append('<span class="counter" id="counter">cycle 0</span>')
    parts.append(
        '<label class="hint">zoom <input id="zoom" type="range" min="25" max="400" '
        'step="5" value="100"></label>'
    )
    parts.append('<span class="hint">&#8592;/&#8594; step, space plays</span>')
    parts.append("</div>")
    parts.append('<div class="frame-state" id="frame-state"></div>')

    parts.append('<div class="figure">')
    parts.append(svg_markup)
    parts.append("</div>")

    parts.append('<section class="legend"><h2>Legend</h2><dl>')
    for term, meaning, swatch in LEGEND_ROWS:
        parts.append(
            '<dt>{}{}</dt><dd>{}</dd>'.format(
                '<i class="sw sw-{}"></i>'.format(swatch) if swatch else "",
                html.escape(term),
                html.escape(meaning),
            )
        )
    parts.append("</dl></section>")
    if footer:
        parts.append("<footer>{}</footer>".format(html.escape(str(footer))))
    parts.append("</main>")
    parts.append(
        '<script id="clock-step-data" type="application/json">{}</script>'.format(
            _embed_json({"frames": frames, "binding": binding})
        )
    )
    parts.append(
        "<script>window.__HAL_CLOCK_STEP__ = JSON.parse("
        'document.getElementById("clock-step-data").textContent);</script>'
    )
    parts.append("<script>")
    parts.append(_JS.rstrip())
    parts.append("</script>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def write_page(path, text):
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return os.path.abspath(str(path))
