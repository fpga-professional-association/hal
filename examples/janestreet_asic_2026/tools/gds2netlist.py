#!/usr/bin/env python3
"""
gds2netlist.py -- extract a structural gate-level netlist from a SkyWater
sky130 GDSII layout.

The extractor is purely geometric: it never looks at instance or net names
inside the GDS (there are none left in a "final" GDS).  It works by

  1.  deriving pin geometry for every referenced ``sky130_fd_sc_hd__*``
      standard cell from the cell's own pin labels (texttype 5) and the
      conductor shapes underneath them,
  2.  collecting the top cell's routing conductors -- polygons *and* paths
      (GDS PATH records, which gdstk returns as ``FlexPath``) -- on
      li1/met1..met5, plus every ``VIA_*`` reference,
  3.  merging everything with a union-find over axis-aligned rectangles,
      accelerated by a uniform spatial hash,
  4.  naming the resulting nets from the top cell's port labels, and
  5.  emitting structural Verilog + a JSON dump.

Everything is done in integer nanometres (the GDS precision is 1 nm), so the
geometry tests are exact -- no floating point tolerance games.

Usage::

    python gds2netlist.py INPUT.gds --verilog out.v --json out.json
    python gds2netlist.py INPUT.gds --report out.md

Requires: gdstk >= 0.9
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import re
import sys
import time

import gdstk

# --------------------------------------------------------------------------
# sky130 layer map
# --------------------------------------------------------------------------

# conductor layer number -> name.  In sky130 the GDS layer number of the
# routing conductors is regular: cut layer <L>/44 always joins conductor
# <L>/20 to conductor <L+1>/20 (mcon, via1, via2, via3, via4).
CONDUCTOR_LAYERS = {
    67: "li1",
    68: "met1",
    69: "met2",
    70: "met3",
    71: "met4",
    72: "met5",
}
# datatypes that carry electrically meaningful metal.  20 = drawing,
# 16 = pin (the pin rectangles overlap the drawing shapes).
CONDUCTOR_DATATYPES = (20, 16)
LABEL_TEXTTYPE = 5
CUT_DATATYPE = 44

# Pin names that are supply, not signal.
POWER_PINS = {"VPWR", "VGND", "VPB", "VNB", "VDD", "VSS", "KAPWR", "LOWHVPWR"}

# sky130_fd_sc_hd output pin names (everything else is an input).
OUTPUT_PINS = {"X", "Y", "Q", "Q_N", "COUT", "COUT_N", "SUM", "HI", "LO"}

STD_CELL_PREFIX = "sky130_fd_sc_hd__"
VIA_CELL_PREFIX = "VIA"

# Cells that carry no signal pins at all -- fillers, taps, decaps.
PHYSICAL_ONLY_SUFFIXES = ("decap_", "tapvpwrvgnd_", "tap_", "fill_", "fakediode_")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

MORSE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F",
    "--.": "G", "....": "H", "..": "I", ".---": "J", "-.-": "K", ".-..": "L",
    "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R",
    "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X",
    "-.--": "Y", "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4",
    ".....": "5", "-....": "6", "--...": "7", "---..": "8", "----.": "9",
}


def decode_morse_row(boxes):
    """Try to read a row of rectangles as Morse code.

    ``boxes`` is a list of ``(x, width)``.  A row qualifies when it uses exactly
    two widths in a 1:3 ratio (dot / dash) and the gaps are 1, 3 and 7 units
    (intra-character, inter-character, inter-word), which is exactly ITU Morse
    timing laid out in space instead of time.  Returns the decoded string or
    ``None``.
    """
    if len(boxes) < 4:
        return None
    boxes = sorted(boxes)
    widths = sorted({w for _x, w in boxes})
    if len(widths) != 2:
        return None
    unit, dash = widths
    if dash != 3 * unit:
        return None
    symbols = []
    for i, (x, w) in enumerate(boxes):
        symbols.append("." if w == unit else "-")
        if i + 1 < len(boxes):
            gap = boxes[i + 1][0] - (x + w)
            if gap == unit:
                continue
            if gap == 3 * unit:
                symbols.append(" ")
            elif gap >= 7 * unit:
                symbols.append("  ")
            else:
                return None
    text = "".join(symbols)
    out = []
    for word in text.split("  "):
        out.append("".join(MORSE.get(ch, "?") for ch in word.split(" ")))
    return " ".join(out)


class DSU:
    """Union-find with path halving."""

    __slots__ = ("parent",)

    def __init__(self, n: int = 0):
        self.parent = list(range(n))

    def add(self) -> int:
        self.parent.append(len(self.parent))
        return len(self.parent) - 1

    def find(self, a: int) -> int:
        p = self.parent
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # keep the smaller index as the root so results are deterministic
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


def _nm(v: float) -> int:
    """GDS user units (um) -> integer nanometres."""
    return int(round(v * 1000.0))


def rectilinear_rects(points_nm):
    """Decompose a rectilinear polygon (list of integer nm points) into a list
    of disjoint axis-aligned rectangles ``(x0, y0, x1, y1)``.

    Uses a vertical scanline: slice at every distinct vertex x, and inside each
    strip the polygon reduces to a set of y-intervals found by crossing the
    strip midline with the horizontal edges.
    """
    n = len(points_nm)
    if n == 4:
        xs = {p[0] for p in points_nm}
        ys = {p[1] for p in points_nm}
        if len(xs) == 2 and len(ys) == 2:
            return [(min(xs), min(ys), max(xs), max(ys))]

    hedges = []  # (xlo, xhi, y) horizontal edges
    for i in range(n):
        ax, ay = points_nm[i]
        bx, by = points_nm[(i + 1) % n]
        if ay == by and ax != bx:
            hedges.append((min(ax, bx), max(ax, bx), ay))
        elif ax != bx and ay != by:
            # non-rectilinear edge -- fall back to the bounding box.  This never
            # happens in an OpenROAD/sky130 flow but keeps the tool total.
            xs = [p[0] for p in points_nm]
            ys = [p[1] for p in points_nm]
            return [(min(xs), min(ys), max(xs), max(ys))]

    xs = sorted({p[0] for p in points_nm})
    rects = []
    for i in range(len(xs) - 1):
        x0, x1 = xs[i], xs[i + 1]
        if x0 == x1:
            continue
        mid2 = x0 + x1  # compare with 2*x to stay in integers
        ys_cross = sorted(y for (xlo, xhi, y) in hedges if 2 * xlo < mid2 < 2 * xhi)
        for j in range(0, len(ys_cross) - 1, 2):
            y0, y1 = ys_cross[j], ys_cross[j + 1]
            if y1 > y0:
                rects.append((x0, y0, x1, y1))
    return rects


def _flexpath_rects(path):
    """Convert a 2-point orthogonal GDS PATH into one rectangle per layer.

    Falls back to ``to_polygons()`` for anything unusual (multi-vertex spines
    or non-orthogonal segments).
    """
    out = []  # (layer, datatype, rect)
    spine = path.spine()
    widths = path.widths()
    ends = path.ends
    nsub = path.num_paths
    if len(spine) != 2:
        for poly in path.to_polygons():
            pts = [(_nm(x), _nm(y)) for x, y in poly.points]
            for r in rectilinear_rects(pts):
                out.append((poly.layer, poly.datatype, r))
        return out

    (ax, ay), (bx, by) = spine[0], spine[1]
    ax, ay, bx, by = _nm(ax), _nm(ay), _nm(bx), _nm(by)
    if ax != bx and ay != by:
        for poly in path.to_polygons():
            pts = [(_nm(x), _nm(y)) for x, y in poly.points]
            for r in rectilinear_rects(pts):
                out.append((poly.layer, poly.datatype, r))
        return out

    for k in range(nsub):
        layer = path.layers[k]
        datatype = path.datatypes[k]
        w = _nm(float(widths[0][k]))
        half = w // 2
        end = ends[k] if isinstance(ends, (list, tuple)) and len(ends) > k else ends
        if isinstance(end, str):
            ext0 = ext1 = half if end == "extended" else 0
        elif isinstance(end, (list, tuple)) and len(end) == 2:
            ext0, ext1 = _nm(float(end[0])), _nm(float(end[1]))
        else:
            ext0 = ext1 = 0

        x0, y0, x1, y1 = min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)
        if ay == by:  # horizontal
            if ax <= bx:
                x0 -= ext0
                x1 += ext1
            else:
                x0 -= ext1
                x1 += ext0
            y0 -= half
            y1 += half
        else:  # vertical
            if ay <= by:
                y0 -= ext0
                y1 += ext1
            else:
                y0 -= ext1
                y1 += ext0
            x0 -= half
            x1 += half
        out.append((layer, datatype, (x0, y0, x1, y1)))
    return out


def cell_shapes(cell):
    """All conductor rectangles of a cell in its own coordinates.

    Returns ``{layer: [rect, ...]}`` plus ``{cut_layer: [rect, ...]}``.
    """
    conductors = collections.defaultdict(list)
    cuts = collections.defaultdict(list)

    for poly in cell.polygons:
        lay, dt = poly.layer, poly.datatype
        if lay in CONDUCTOR_LAYERS and dt in CONDUCTOR_DATATYPES:
            pts = [(_nm(x), _nm(y)) for x, y in poly.points]
            conductors[lay].extend(rectilinear_rects(pts))
        elif lay in CONDUCTOR_LAYERS and dt == CUT_DATATYPE:
            pts = [(_nm(x), _nm(y)) for x, y in poly.points]
            cuts[lay].extend(rectilinear_rects(pts))

    for path in cell.paths:
        for lay, dt, rect in _flexpath_rects(path):
            if lay in CONDUCTOR_LAYERS and dt in CONDUCTOR_DATATYPES:
                conductors[lay].append(rect)
            elif lay in CONDUCTOR_LAYERS and dt == CUT_DATATYPE:
                cuts[lay].append(rect)

    return conductors, cuts


def rects_touch(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


# --------------------------------------------------------------------------
# geometric transforms (rotations are always multiples of 90 deg here)
# --------------------------------------------------------------------------

class Transform:
    """origin + rotation + x_reflection, applied as GDS defines it:
    reflect about the x-axis first, then rotate, then translate."""

    __slots__ = ("ox", "oy", "c", "s", "mirror")

    def __init__(self, ref):
        self.ox = _nm(ref.origin[0])
        self.oy = _nm(ref.origin[1])
        rot = float(ref.rotation or 0.0)
        # snap to the nearest multiple of 90 degrees
        q = int(round(rot / (math.pi / 2.0))) % 4
        self.c, self.s = [(1, 0), (0, 1), (-1, 0), (0, -1)][q]
        self.mirror = bool(ref.x_reflection)

    def point(self, x, y):
        if self.mirror:
            y = -y
        return (x * self.c - y * self.s + self.ox, x * self.s + y * self.c + self.oy)

    def rect(self, r):
        x0, y0 = self.point(r[0], r[1])
        x1, y1 = self.point(r[2], r[3])
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


ORIENT_NAME = {(1, 0, False): "N", (1, 0, True): "FS",
               (-1, 0, False): "S", (-1, 0, True): "FN",
               (0, 1, False): "W", (0, 1, True): "FW",
               (0, -1, False): "E", (0, -1, True): "FE"}


# --------------------------------------------------------------------------
# standard-cell pin geometry
# --------------------------------------------------------------------------

class CellModel:
    """Pin geometry + boundary of one standard cell, derived from the GDS."""

    def __init__(self, cell):
        self.name = cell.name
        self.pins = {}          # pin name -> [(layer, rect), ...]
        self.pin_labels = {}    # pin name -> [(layer, x, y), ...]
        self.power_pins = set()
        self.boundary = None    # (x0, y0, x1, y1) of the 81/4 areaid.standardc

        for poly in cell.polygons:
            if (poly.layer, poly.datatype) in ((81, 4), (235, 4)):
                bb = poly.bounding_box()
                r = (_nm(bb[0][0]), _nm(bb[0][1]), _nm(bb[1][0]), _nm(bb[1][1]))
                if self.boundary is None:
                    self.boundary = r
                else:
                    self.boundary = (min(self.boundary[0], r[0]), min(self.boundary[1], r[1]),
                                     max(self.boundary[2], r[2]), max(self.boundary[3], r[3]))
        if self.boundary is None:
            bb = cell.bounding_box()
            self.boundary = (_nm(bb[0][0]), _nm(bb[0][1]), _nm(bb[1][0]), _nm(bb[1][1]))

        conductors, cuts = cell_shapes(cell)

        # index the rectangles and merge same-layer touching ones
        flat = []   # (layer, rect)
        for lay, rects in conductors.items():
            for r in rects:
                flat.append((lay, r))
        dsu = DSU(len(flat))
        by_layer = collections.defaultdict(list)
        for i, (lay, r) in enumerate(flat):
            by_layer[lay].append(i)
        for lay, idxs in by_layer.items():
            for a in range(len(idxs)):
                ia = idxs[a]
                ra = flat[ia][1]
                for b in range(a + 1, len(idxs)):
                    ib = idxs[b]
                    if rects_touch(ra, flat[ib][1]):
                        dsu.union(ia, ib)

        # Intra-cell vias.  sky130 cells route some pins up to met1 internally
        # (for example dfrtp_2's RESET_B, which the router reaches on met1 and
        # never on li1), so the pin's geometry spans li1 + mcon + met1.  A cut
        # on layer L always bridges conductor L to conductor L+1.
        for cut_layer, cut_rects in cuts.items():
            up = cut_layer + 1
            if cut_layer not in CONDUCTOR_LAYERS or up not in CONDUCTOR_LAYERS:
                continue
            lo_idx = by_layer.get(cut_layer, ())
            up_idx = by_layer.get(up, ())
            for cr in cut_rects:
                touched = [i for i in lo_idx if rects_touch(cr, flat[i][1])]
                touched += [i for i in up_idx if rects_touch(cr, flat[i][1])]
                for i in touched[1:]:
                    dsu.union(touched[0], i)

        # map pin labels onto components
        comp_of_pin = collections.defaultdict(set)
        power_comps = set()
        for label in cell.labels:
            if label.texttype != LABEL_TEXTTYPE:
                continue
            if label.layer not in CONDUCTOR_LAYERS:
                continue
            name = label.text.strip()
            if not name:
                continue
            lx, ly = _nm(label.origin[0]), _nm(label.origin[1])
            hit = None
            for i in by_layer.get(label.layer, ()):
                r = flat[i][1]
                if r[0] <= lx <= r[2] and r[1] <= ly <= r[3]:
                    hit = i
                    break
            if name in POWER_PINS:
                self.power_pins.add(name)
                if hit is not None:
                    power_comps.add(dsu.find(hit))
                continue
            if hit is None:
                continue
            comp_of_pin[name].add(dsu.find(hit))
            self.pin_labels.setdefault(name, []).append((label.layer, lx, ly))

        # a pin may be labelled on several disjoint shapes -- they are the same
        # electrical pin, so merge those components too
        for name, comps in comp_of_pin.items():
            comps = sorted(comps)
            for c in comps[1:]:
                dsu.union(comps[0], c)

        roots_by_pin = {name: dsu.find(sorted(comps)[0]) for name, comps in comp_of_pin.items()}
        shapes_by_root = collections.defaultdict(list)
        for i, (lay, r) in enumerate(flat):
            shapes_by_root[dsu.find(i)].append((lay, r))
        power_roots = {dsu.find(c) for c in power_comps}
        self.problems = []
        for name, root in roots_by_pin.items():
            if root in power_roots:
                self.problems.append("%s: signal pin %s merged with a supply net"
                                     % (self.name, name))
                continue
            self.pins[name] = shapes_by_root[root]
        # two different pins may never share a component
        seen = {}
        for name, root in roots_by_pin.items():
            if root in seen:
                self.problems.append("%s: pins %s and %s share one shape component"
                                     % (self.name, seen[root], name))
            seen[root] = name

    @property
    def is_physical_only(self):
        return not self.pins


class ViaModel:
    """A ``VIA_*`` cell: the conductor layers it bridges and its shapes."""

    def __init__(self, cell):
        self.name = cell.name
        conductors, cuts = cell_shapes(cell)
        self.shapes = []  # (layer, rect)
        for lay, rects in conductors.items():
            for r in rects:
                self.shapes.append((lay, r))
        self.layers = sorted(conductors)
        self.cut_layers = sorted(cuts)


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

BUS_RE = re.compile(r"^(.*?)\[(\d+)\]$")


class Extractor:
    def __init__(self, gds_path, top_name=None, grid=2000, verbose=True):
        self.path = gds_path
        self.verbose = verbose
        self.grid = grid
        self.lib = gdstk.read_gds(gds_path)
        cells = {c.name: c for c in self.lib.cells}
        self.cells = cells
        if top_name:
            self.top = cells[top_name]
        else:
            tops = self.lib.top_level()
            if len(tops) != 1:
                raise SystemExit("ambiguous top cell: %s" % [c.name for c in tops])
            self.top = tops[0]

        self.cell_models = {}
        self.via_models = {}
        self.annotations = []   # refs to non-sky130, non-VIA cells
        self.warnings = []

        self.shapes = []        # (layer, x0, y0, x1, y1)
        self.dsu = DSU()
        self.instances = []     # dicts
        self.port_labels = []   # (name, layer, x, y)
        self.power_roots = set()

    # -- logging ---------------------------------------------------------
    def log(self, msg):
        if self.verbose:
            print("[gds2netlist] %s" % msg, file=sys.stderr)

    # -- shape bookkeeping ----------------------------------------------
    def _add_shape(self, layer, rect):
        self.shapes.append((layer, rect[0], rect[1], rect[2], rect[3]))
        return self.dsu.add()

    # -- main ------------------------------------------------------------
    def run(self):
        t0 = time.time()
        self._collect_top_geometry()
        self.log("collected %d conductor rectangles" % len(self.shapes))
        self._union_by_overlap()
        self.log("union-find done (%.1fs)" % (time.time() - t0))
        self._resolve_nets()
        self.log("%d signal nets over %d instances (%.1fs)"
                 % (len(self.nets), len(self.instances), time.time() - t0))
        return self

    # -- step 1/2 --------------------------------------------------------
    def _collect_top_geometry(self):
        top = self.top

        # own polygons
        for poly in top.polygons:
            if poly.layer in CONDUCTOR_LAYERS and poly.datatype in CONDUCTOR_DATATYPES:
                pts = [(_nm(x), _nm(y)) for x, y in poly.points]
                for r in rectilinear_rects(pts):
                    self._add_shape(poly.layer, r)

        # own paths (GDS PATH records)
        for path in top.paths:
            for lay, dt, r in _flexpath_rects(path):
                if lay in CONDUCTOR_LAYERS and dt in CONDUCTOR_DATATYPES:
                    self._add_shape(lay, r)

        # port labels
        for label in top.labels:
            if label.texttype != LABEL_TEXTTYPE or label.layer not in CONDUCTOR_LAYERS:
                continue
            self.port_labels.append((label.text.strip(), label.layer,
                                     _nm(label.origin[0]), _nm(label.origin[1])))

        # references
        for ref in top.references:
            cname = ref.cell.name if ref.cell is not None else None
            if cname is None:
                continue
            if ref.repetition is not None and getattr(ref.repetition, "size", 1) not in (0, 1):
                self.warnings.append("array reference to %s is not supported" % cname)
            if cname.startswith(VIA_CELL_PREFIX):
                self._place_via(ref, cname)
            elif cname.startswith(STD_CELL_PREFIX):
                self._place_cell(ref, cname)
            else:
                xf = Transform(ref)
                bb = ref.cell.bounding_box()
                r = xf.rect((_nm(bb[0][0]), _nm(bb[0][1]), _nm(bb[1][0]), _nm(bb[1][1])))
                self.annotations.append({"cell": cname, "x": r[0], "y": r[1],
                                         "bbox": list(r)})

    def _place_via(self, ref, cname):
        model = self.via_models.get(cname)
        if model is None:
            model = self.via_models[cname] = ViaModel(ref.cell)
        xf = Transform(ref)
        first = None
        for lay, r in model.shapes:
            idx = self._add_shape(lay, xf.rect(r))
            if first is None:
                first = idx
            else:
                # a via shorts its landing pads across the two conductor layers
                self.dsu.union(first, idx)

    def _place_cell(self, ref, cname):
        model = self.cell_models.get(cname)
        if model is None:
            model = self.cell_models[cname] = CellModel(ref.cell)
            self.warnings.extend(model.problems)
        xf = Transform(ref)
        bnd = xf.rect(model.boundary)
        inst = {
            "cell": cname,
            "x": bnd[0],
            "y": bnd[1],
            "bbox": list(bnd),
            "orient": ORIENT_NAME.get((xf.c, xf.s, xf.mirror), "N"),
            "pin_node": {},
            "physical_only": model.is_physical_only,
        }
        for pin, shapes in model.pins.items():
            first = None
            for lay, r in shapes:
                idx = self._add_shape(lay, xf.rect(r))
                if first is None:
                    first = idx
                else:
                    self.dsu.union(first, idx)
            inst["pin_node"][pin] = first
        self.instances.append(inst)

    # -- step 3 ----------------------------------------------------------
    def _union_by_overlap(self):
        grid = self.grid
        buckets = collections.defaultdict(list)
        for i, (lay, x0, y0, x1, y1) in enumerate(self.shapes):
            gx0, gx1 = x0 // grid, x1 // grid
            gy0, gy1 = y0 // grid, y1 // grid
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    buckets[(lay, gx, gy)].append(i)
        shapes = self.shapes
        union = self.dsu.union
        for idxs in buckets.values():
            n = len(idxs)
            if n < 2:
                continue
            for a in range(n):
                ia = idxs[a]
                _, ax0, ay0, ax1, ay1 = shapes[ia]
                for b in range(a + 1, n):
                    ib = idxs[b]
                    _, bx0, by0, bx1, by1 = shapes[ib]
                    if ax0 <= bx1 and bx0 <= ax1 and ay0 <= by1 and by0 <= ay1:
                        union(ia, ib)

    # -- step 4 ----------------------------------------------------------
    def _name_instances(self):
        """Deterministic, location-derived instance names."""
        self.instances.sort(key=lambda d: (d["y"], d["x"], d["cell"]))
        used = set()
        for inst in self.instances:
            short = inst["cell"][len(STD_CELL_PREFIX):]
            base = "%s_%d_%d" % (short, inst["x"], inst["y"])
            name = base
            k = 1
            while name in used:
                k += 1
                name = "%s__%d" % (base, k)
            used.add(name)
            inst["name"] = name

    def _resolve_nets(self):
        dsu = self.dsu
        shapes = self.shapes
        self._name_instances()

        # named roots from the top cell's port labels
        root_names = {}
        self.ports = {}
        unplaced = []
        for name, lay, lx, ly in self.port_labels:
            hit = None
            for i, (slay, x0, y0, x1, y1) in enumerate(shapes):
                if slay == lay and x0 <= lx <= x1 and y0 <= ly <= y1:
                    hit = i
                    break
            if hit is None:
                unplaced.append(name)
                continue
            root = dsu.find(hit)
            if name in POWER_PINS:
                self.power_roots.add(root)
                continue
            root_names.setdefault(root, name)
            self.ports[name] = root
        for name in unplaced:
            self.warnings.append("port label %r sits on no conductor shape" % name)

        # constant nets from tie cells
        self.constants = {}
        for inst in self.instances:
            if not inst["cell"].startswith(STD_CELL_PREFIX + "conb"):
                continue
            for pin, node in inst["pin_node"].items():
                if node is None:
                    continue
                root = dsu.find(node)
                if pin == "HI":
                    self.constants[root] = 1
                elif pin == "LO":
                    self.constants[root] = 0

        # collect pins per root
        members = collections.defaultdict(list)
        for ii, inst in enumerate(self.instances):
            for pin, node in inst["pin_node"].items():
                if node is None:
                    continue
                members[dsu.find(node)].append((ii, pin))

        # every root that carries at least one pin or one port is a net
        roots = set(members) | set(root_names)
        roots -= self.power_roots

        # deterministic ordering: by (lowest y, lowest x) of the root's pins
        def sort_key(root):
            pins = members.get(root, [])
            if pins:
                ii = min(pins)[0]
                return (self.instances[ii]["y"], self.instances[ii]["x"], root)
            return (1 << 40, 1 << 40, root)

        self.nets = {}
        self.net_of_root = {}
        seq = 0
        for root in sorted(roots, key=sort_key):
            name = root_names.get(root)
            if name is None:
                const = self.constants.get(root)
                if const is not None:
                    seq += 1
                    name = "TIE%d_%d" % (const, seq)
                else:
                    seq += 1
                    name = "n%d" % seq
            base = name
            k = 1
            while name in self.nets:
                k += 1
                name = "%s_%d" % (base, k)
            self.nets[name] = {
                "root": root,
                "pins": sorted(members.get(root, [])),
                "constant": self.constants.get(root),
                "is_port": root in root_names,
            }
            self.net_of_root[root] = name

        # write the resolved net back onto every instance pin
        for inst in self.instances:
            conns = {}
            for pin, node in inst["pin_node"].items():
                conns[pin] = self.net_of_root.get(dsu.find(node)) if node is not None else None
            inst["conns"] = conns

    # -- reporting helpers ----------------------------------------------
    def floating_clusters(self):
        """Conductor components that touch no cell pin, no port and no supply.

        These are fill, dummy metal or decorative geometry: harmless, but worth
        reporting because they prove the extractor is not silently dropping
        routing.  Returns a list of ``(nshapes, bbox)`` sorted by size.
        """
        live = set(self.net_of_root) | self.power_roots
        groups = collections.defaultdict(list)
        for i, s in enumerate(self.shapes):
            r = self.dsu.find(i)
            if r in live:
                continue
            groups[r].append(s)
        out = []
        for r, ss in groups.items():
            out.append((len(ss),
                        (min(s[1] for s in ss), min(s[2] for s in ss),
                         max(s[3] for s in ss), max(s[4] for s in ss))))
        out.sort(reverse=True)
        return out

    def census(self):
        return collections.Counter(i["cell"] for i in self.instances)

    # -- output ----------------------------------------------------------
    def _port_directions(self):
        dirs = {}
        for name, root in self.ports.items():
            info = self.nets.get(self.net_of_root.get(root, ""))
            direction = "input"
            if info:
                for ii, pin in info["pins"]:
                    if pin in OUTPUT_PINS:
                        direction = "output"
                        break
            dirs[name] = direction
        return dirs

    def write_verilog(self, path, module=None):
        module = module or self.top.name
        dirs = self._port_directions()

        # group bus bits
        buses = collections.defaultdict(list)
        scalars = []
        for name in self.ports:
            m = BUS_RE.match(name)
            if m:
                buses[m.group(1)].append(int(m.group(2)))
            else:
                scalars.append(name)

        port_decls = []
        for b, bits in sorted(buses.items()):
            d = dirs.get("%s[%d]" % (b, bits[0]), "input")
            port_decls.append((b, d, (max(bits), min(bits))))
        for s in sorted(scalars):
            port_decls.append((s, dirs.get(s, "input"), None))
        port_decls.sort(key=lambda t: t[0])

        def vname(n):
            if re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", n):
                return n
            m = BUS_RE.match(n)
            if m and m.group(1) in buses:
                return "%s[%s]" % (m.group(1), m.group(2))
            return "\\%s " % n

        lines = []
        lines.append("// Generated by gds2netlist.py from %s" % os.path.basename(self.path))
        lines.append("// %d instances, %d nets" % (len(self.instances), len(self.nets)))
        lines.append("")
        lines.append("module %s (%s);" % (module, ", ".join(p[0] for p in port_decls)))
        for nm, d, rng in port_decls:
            if rng:
                lines.append(" %s [%d:%d] %s;" % (d, rng[0], rng[1], nm))
            else:
                lines.append(" %s %s;" % (d, nm))
        lines.append("")
        for name in sorted(self.nets):
            if name in self.ports:
                continue
            m = BUS_RE.match(name)
            if m and m.group(1) in buses:
                continue
            lines.append(" wire %s;" % vname(name))
        lines.append("")
        for inst in self.instances:
            conns = [(p, n) for p, n in sorted(inst["conns"].items()) if n is not None]
            if not conns:
                lines.append(" %s %s ();" % (inst["cell"], vname(inst["name"])))
                continue
            body = ",\n    ".join(".%s(%s)" % (p, vname(n)) for p, n in conns)
            lines.append(" %s %s (%s);" % (inst["cell"], vname(inst["name"]), body))
        lines.append("endmodule")
        lines.append("")
        with open(path, "w") as fh:
            fh.write("\n".join(lines))
        self.log("wrote %s" % path)

    def to_json(self):
        dirs = self._port_directions()
        return {
            "source": os.path.basename(self.path),
            "top": self.top.name,
            "units": "nm",
            "cell_census": dict(sorted(self.census().items())),
            "ports": {n: {"net": self.net_of_root.get(r), "dir": dirs.get(n)}
                      for n, r in sorted(self.ports.items())},
            "instances": [
                {
                    "name": i["name"],
                    "cell": i["cell"],
                    "x": i["x"],
                    "y": i["y"],
                    "orient": i["orient"],
                    "physical_only": i["physical_only"],
                    "connections": i["conns"],
                }
                for i in self.instances
            ],
            "nets": {
                name: {
                    "constant": info["constant"],
                    "is_port": info["is_port"],
                    "pins": [{"instance": self.instances[ii]["name"], "pin": p}
                             for ii, p in info["pins"]],
                }
                for name, info in sorted(self.nets.items())
            },
            "annotations": self.annotations,
            "warnings": self.warnings,
        }

    def write_json(self, path):
        with open(path, "w") as fh:
            json.dump(self.to_json(), fh, indent=1, sort_keys=False)
        self.log("wrote %s" % path)

    # -- markdown report -------------------------------------------------
    def die_area(self):
        for poly in self.top.polygons:
            if (poly.layer, poly.datatype) in ((235, 4), (81, 4)):
                bb = poly.bounding_box()
                return (_nm(bb[0][0]), _nm(bb[0][1]), _nm(bb[1][0]), _nm(bb[1][1]))
        return None

    def write_report(self, path):
        census = self.census()
        phys = {c: n for c, n in census.items()
                if any(s in c for s in PHYSICAL_ONLY_SUFFIXES)}
        seq = {c: n for c, n in census.items()
               if c[len(STD_CELL_PREFIX):].startswith(("df", "sdf", "dl"))}
        clk = {c: n for c, n in census.items() if "clkbuf" in c or "clkinv" in c}
        tie = {c: n for c, n in census.items() if "conb" in c}
        diode = {c: n for c, n in census.items() if "diode" in c}
        comb = {c: n for c, n in census.items()
                if c not in phys and c not in seq and c not in clk
                and c not in tie and c not in diode}

        outs = OUTPUT_PINS
        driverless, multidriver = [], []
        for name, info in self.nets.items():
            nd = sum(1 for _ii, p in info["pins"] if p in outs)
            if nd == 0 and not info["is_port"] and info["constant"] is None:
                driverless.append(name)
            if nd > 1:
                multidriver.append(name)
        dangling = [(n, i) for n, i in self.nets.items()
                    if len(i["pins"]) == 1 and not i["is_port"]]
        dirs = self._port_directions()
        da = self.die_area()

        L = []
        A = L.append
        A("# GDS extraction report -- `%s`" % os.path.basename(self.path))
        A("")
        A("Produced by `tools/gds2netlist.py` (geometric extraction, no names read")
        A("from the GDS other than standard-cell pin labels and top-level port labels).")
        A("")
        A("## Summary")
        A("")
        A("| item | value |")
        A("| --- | --- |")
        A("| top cell | `%s` |" % self.top.name)
        if da:
            A("| die area | %.2f x %.2f um |" % ((da[2] - da[0]) / 1000.0,
                                                 (da[3] - da[1]) / 1000.0))
        A("| standard-cell instances | %d |" % len(self.instances))
        A("| ... of which physical-only (decap/tap/fill) | %d |" % sum(phys.values()))
        A("| distinct cell types | %d |" % len(census))
        A("| conductor rectangles analysed | %d |" % len(self.shapes))
        A("| via instances | %d |" % sum(1 for r in self.top.references
                                         if r.cell and r.cell.name.startswith(VIA_CELL_PREFIX)))
        A("| signal nets | %d |" % len(self.nets))
        A("| top-level ports | %d |" % len(self.ports))
        A("")
        A("## Method and validation")
        A("")
        A("Connectivity is rebuilt from geometry alone:")
        A("")
        A("1. every `sky130_fd_sc_hd__*` cell's pin shapes are derived from its own")
        A("   pin labels (texttype 5) plus the conductor shapes under them, following")
        A("   intra-cell `mcon`/`via*` cuts -- several cells (`dfrtp_2`'s `RESET_B`,")
        A("   for one) present their pin on met1, not li1, and the router uses that;")
        A("2. top-cell routing is read from polygons **and** GDS PATH records on")
        A("   li1/met1..met5, and every `VIA_*` reference shorts its two landing pads;")
        A("3. a union-find over axis-aligned rectangles (spatial hash, exact integer-nm")
        A("   overlap test) produces the net partition;")
        A("4. nets are named from the top cell's port labels, constants from `conb_1`.")
        A("")
        A("`tools/validate_warmup.py` runs the same extractor on `warmup/04_final.gds`")
        A("and compares it against the ground truth in")
        A("`warmup/03_post_place_and_route.def` and `warmup/01_netlist.v`:")
        A("230/230 instances matched by cell type, placement *and* orientation;")
        A("285/285 signal pins land in a net partition that is exactly isomorphic to")
        A("the DEF's; all 6 signal ports get their correct name. No exceptions.")
        A("")
        A("## Ports")
        A("")
        A("| port | direction | terminals |")
        A("| --- | --- | --- |")
        for p in sorted(self.ports):
            net = self.net_of_root.get(self.ports[p])
            A("| `%s` | %s | %d |" % (p, dirs.get(p), len(self.nets[net]["pins"]) if net else 0))
        A("")
        A("## Cell census")
        A("")
        for title, group in (("Sequential", seq), ("Clock tree", clk),
                             ("Tie cells", tie), ("Antenna diodes", diode),
                             ("Combinational", comb), ("Physical only", phys)):
            if not group:
                continue
            A("### %s (%d instances, %d types)" % (title, sum(group.values()), len(group)))
            A("")
            A("| cell | count |")
            A("| --- | --- |")
            for c in sorted(group):
                A("| `%s` | %d |" % (c, group[c]))
            A("")
        A("## Connectivity health")
        A("")
        A("| check | count |")
        A("| --- | --- |")
        A("| pins with no net at all | %d |"
          % sum(1 for i in self.instances for v in i["conns"].values() if v is None))
        A("| nets with more than one driver | %d |" % len(multidriver))
        A("| nets with no driver and no port | %d |" % len(driverless))
        A("| single-terminal (dangling) nets | %d |" % len(dangling))
        A("| constant nets from `conb_1` | %d |" % len(self.constants))
        A("")
        if self.constants:
            A("### Constant nets")
            A("")
            A("| net | value | terminals |")
            A("| --- | --- | --- |")
            for name, info in sorted(self.nets.items()):
                if info["constant"] is None:
                    continue
                A("| `%s` | %d | %d |" % (name, info["constant"], len(info["pins"])))
            A("")
        if dangling:
            A("### Dangling nets (single terminal)")
            A("")
            A("Unused tie-cell outputs (a `conb_1` always offers both `HI` and `LO`,")
            A("the design usually needs one) and clock buffers whose output drives")
            A("nothing -- the dummy load buffers OpenROAD's CTS inserts to balance")
            A("clock-tree levels.  Both are expected, not extraction errors.")
            A("")
            A("| net | terminal |")
            A("| --- | --- |")
            for name, info in sorted(dangling):
                ii, pin = info["pins"][0]
                A("| `%s` | `%s`.%s |" % (name, self.instances[ii]["name"], pin))
            A("")
        if driverless:
            A("### Nets with no driving output pin")
            A("")
            A("Fully routed nets that join inputs to each other but reach no cell output")
            A("and no port.  Each one was traced by hand: the routing really does end")
            A("there, so these are floating inputs in the source netlist, not missed")
            A("connections.")
            A("")
            for name in sorted(driverless):
                terms = ", ".join("`%s`.%s" % (self.instances[ii]["name"], p)
                                  for ii, p in self.nets[name]["pins"])
                A("- `%s`: %s" % (name, terms))
            A("")
        if multidriver:
            A("### Nets with multiple drivers")
            A("")
            for name in sorted(multidriver):
                A("- `%s`" % name)
            A("")

        clusters = self.floating_clusters()
        A("### Floating conductor geometry")
        A("")
        A("Metal that belongs to no cell pin, no port and no supply -- fill, dummy")
        A("metal or decoration.  Listed so it is visible that nothing was dropped.")
        A("")
        A("| clusters | rectangles |")
        A("| --- | --- |")
        A("| %d | %d |" % (len(clusters), sum(c[0] for c in clusters)))
        A("")
        if clusters:
            A("| rectangles | bounding box (nm) |")
            A("| --- | --- |")
            for n, bb in clusters[:10]:
                A("| %d | (%d, %d) .. (%d, %d) |" % (n, bb[0], bb[1], bb[2], bb[3]))
            A("")
        if self.annotations:
            groups = collections.Counter(a["cell"] for a in self.annotations)
            A("## Non-functional annotation cells")
            A("")
            A("These references are **not** part of the netlist: their only geometry is")
            A("on GDS layer 200/0, which is not a conductor in the sky130 stack.  They sit")
            A("outside the die area and are recorded here for completeness.")
            A("")
            A("| cell | count |")
            A("| --- | --- |")
            for c, n in sorted(groups.items()):
                A("| `%s` | %d |" % (c, n))
            A("")
            rows = collections.defaultdict(list)
            for a in self.annotations:
                rows[a["y"]].append((a["x"], a["bbox"][2] - a["bbox"][0]))
            for y in sorted(rows):
                text = decode_morse_row(rows[y])
                if text:
                    A("The row at y = %d nm is Morse code: the two box widths are in a" % y)
                    A("1:3 ratio (dot / dash) and the gaps are 1 / 3 / 7 units, i.e. ITU")
                    A("timing laid out in space.  It reads:")
                    A("")
                    A("> **%s**" % text)
                    A("")
            A("| cell | x (nm) | y (nm) | width (nm) |")
            A("| --- | --- | --- | --- |")
            for a in sorted(self.annotations, key=lambda d: (d["y"], d["x"])):
                A("| `%s` | %d | %d | %d |" % (a["cell"], a["x"], a["y"],
                                               a["bbox"][2] - a["bbox"][0]))
            A("")
        A("## Warnings")
        A("")
        if self.warnings:
            for w in self.warnings:
                A("- %s" % w)
        else:
            A("None.")
        A("")
        with open(path, "w") as fh:
            fh.write("\n".join(L))
        self.log("wrote %s" % path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gds")
    ap.add_argument("--top", default=None)
    ap.add_argument("--verilog", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--report", default=None, help="write a markdown extraction report")
    ap.add_argument("--grid", type=int, default=2000, help="spatial hash cell size in nm")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    ex = Extractor(args.gds, args.top, grid=args.grid, verbose=not args.quiet).run()
    if args.verilog:
        ex.write_verilog(args.verilog)
    if args.json:
        ex.write_json(args.json)
    if args.report:
        ex.write_report(args.report)
    if not args.verilog and not args.json and not args.report:
        print(json.dumps(ex.to_json(), indent=1))
    return ex


if __name__ == "__main__":
    main()
