"""Static, self-contained HTML reports from findings documents and hal_viz artifacts.

The input is the shared ``tools/hal_findings`` contract (one JSON document per
analysis run) plus the files hal_viz already produced -- ``.dot``, ``.svg``,
``.png``, group listings, waveforms, witnesses.  Nothing here re-derives a
graph: scoped diagrams come from :mod:`hal_viz.extract` via the ordinary
``netlist_graph`` command, or from whatever exporter the analysis used, and are
referenced from the findings document as evidence.

Three rules drive the whole module:

* **Nothing is quietly dropped.** Bounded claims, truncated lists, unsupported
  primitives, unknown/timeout/error results, missing evidence files and
  documents that fail schema validation all get a visible marker.  A report that
  silently hides a limitation is worse than no report.
* **Every netlist-derived string is untrusted.** Gate, net, module and design
  names come from third-party HDL and go through :func:`text` (HTML escaping,
  control characters neutralized) before they reach the page; embedded SVG goes
  through :func:`sanitize_svg`.
* **The page opens offline.** Styles are inline, images are inlined SVG or
  relative links, and no element ever points at a remote host.
"""

import html
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from urllib.parse import quote as _url_quote

from . import __version__
from .render import RenderError, find_dot_binary, render_dot

__all__ = [
    "ReportError",
    "STATUS_META",
    "STATUS_ORDER",
    "LoadedDocument",
    "ReportOptions",
    "text",
    "sanitize_svg",
    "highlight_svg",
    "scope_labels",
    "relative_href",
    "load_document",
    "load_documents",
    "build_report",
    "write_report",
]


class ReportError(RuntimeError):
    """A document could not be read, or a report could not be written."""


# ---------------------------------------------------------------------------
# status presentation
# ---------------------------------------------------------------------------

#: How each of the nine ``hal_findings`` statuses is presented.  ``glyph`` and
#: ``label`` carry the distinction on their own, so the nine are still told
#: apart in greyscale or by a colour-blind reader; ``css`` only adds colour.
#: ``sort`` puts the results that need attention first.
STATUS_META = {
    "counterexample": {
        "sort": 0,
        "label": "REFUTED",
        "glyph": "✘",
        "css": "refuted",
        "claim": "refuted for all executions; no cycle bound involved",
    },
    "bounded_counterexample": {
        "sort": 1,
        "label": "REFUTED (bounded)",
        "glyph": "✘≤",
        "css": "refuted-bounded",
        "claim": "refuted within the stated cycle bound",
    },
    "error": {
        "sort": 2,
        "label": "ERROR",
        "glyph": "⚠",
        "css": "error",
        "claim": "the analysis itself failed; this says nothing about the design",
    },
    "timeout": {
        "sort": 3,
        "label": "TIMEOUT",
        "glyph": "⏱",
        "css": "timeout",
        "claim": "aborted at a resource limit; no verdict",
    },
    "unknown": {
        "sort": 4,
        "label": "UNKNOWN",
        "glyph": "?",
        "css": "unknown",
        "claim": "no verdict was reached",
    },
    "unsupported": {
        "sort": 5,
        "label": "UNSUPPORTED",
        "glyph": "⊘",
        "css": "unsupported",
        "claim": "outside what the analysis covers; absence of a result is not a result",
    },
    "heuristic": {
        "sort": 6,
        "label": "HEURISTIC",
        "glyph": "≈",
        "css": "heuristic",
        "claim": "evidence-based guess, never a proof",
    },
    "proven_bounded": {
        "sort": 7,
        "label": "PROVEN (bounded)",
        "glyph": "✔≤",
        "css": "proven-bounded",
        "claim": "holds only up to the stated cycle bound",
    },
    "proven_under_assumptions": {
        "sort": 8,
        "label": "PROVEN",
        "glyph": "✔",
        "css": "proven",
        "claim": "holds for every execution, given the listed assumptions",
    },
}

#: Statuses in presentation order.
STATUS_ORDER = tuple(
    sorted(STATUS_META, key=lambda status: STATUS_META[status]["sort"])
)

_UNKNOWN_STATUS = {
    "sort": 99,
    "label": "UNRECOGNIZED STATUS",
    "glyph": "!",
    "css": "unrecognized",
    "claim": "this status is not part of the findings schema this report understands",
}


def status_meta(status):
    """Presentation metadata for ``status``; unknown statuses stay visible."""
    return STATUS_META.get(status, _UNKNOWN_STATUS)


# ---------------------------------------------------------------------------
# escaping
# ---------------------------------------------------------------------------

# C0/C1 controls except tab and newline. Gate names come out of third-party HDL
# and may contain anything at all; a raw \x00 or \x1b in the page is at best
# unreadable and at worst a terminal escape when the HTML is catted.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def text(value):
    """Escape ``value`` for use as HTML text *or* inside a quoted attribute.

    Everything that reaches the page goes through here: names are untrusted
    input, so ``<``, ``>``, ``&``, ``"`` and ``'`` are always escaped and
    control characters are replaced with U+FFFD.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return html.escape(_CONTROL_RE.sub("�", value), quote=True)


def _scalar(value):
    """Render a scalar the way a report reader expects (JSON-ish booleans)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _json_text(value, limit=20000):
    """Pretty-print a JSON value for display, escaped and length-capped."""
    try:
        rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = repr(value)
    truncated = len(rendered) > limit
    if truncated:
        rendered = rendered[:limit] + "\n... truncated"
    return text(rendered)


# ---------------------------------------------------------------------------
# SVG embedding
# ---------------------------------------------------------------------------

# Elements that can execute or fetch something. Graphviz never emits them, but
# an evidence SVG can come from any analysis, so they are removed rather than
# trusted.
_ACTIVE_ELEMENTS = (
    "script",
    "foreignObject",
    "iframe",
    "object",
    "embed",
    "animate",
    "animateTransform",
    "animateMotion",
    "set",
    "handler",
    "audio",
    "video",
)
_PAIRED_ACTIVE_RE = re.compile(
    r"<\s*(" + "|".join(_ACTIVE_ELEMENTS) + r")\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_LONE_ACTIVE_RE = re.compile(
    r"<\s*/?\s*(" + "|".join(_ACTIVE_ELEMENTS) + r")\b[^>]*>",
    re.IGNORECASE,
)
_EVENT_ATTR_RE = re.compile(
    r"\son[a-zA-Z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE
)
_HREF_ATTR_RE = re.compile(
    r"\s(?:xlink:)?href\s*=\s*(\"([^\"]*)\"|'([^']*)')", re.IGNORECASE
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_DOCTYPE_RE = re.compile(r"<!DOCTYPE.*?>", re.IGNORECASE | re.DOTALL)
_XMLDECL_RE = re.compile(r"<\?xml.*?\?>", re.DOTALL)
_ROOT_SIZE_RE = re.compile(r'\s(width|height)\s*=\s*("[^"]*"|\'[^\']*\')', re.IGNORECASE)


def _strip_remote_hrefs(match):
    value = match.group(2) if match.group(2) is not None else match.group(3)
    # Only in-document fragment references survive; everything else could be a
    # network fetch or a javascript: URL.
    if value.startswith("#"):
        return match.group(0)
    return " "


def sanitize_svg(svg_text):
    """Return an inlinable fragment of ``svg_text``, or ``None``.

    The document prologue (XML declaration, DOCTYPE, comments) is dropped
    because an inline fragment may not carry it, scripts and event handlers are
    removed, and every ``href``/``xlink:href`` that is not a local fragment is
    stripped so that an embedded picture can never reach the network.  The root
    element's ``width``/``height`` are dropped when a ``viewBox`` is present so
    the drawing scales to the report column.
    """
    if not svg_text:
        return None
    lowered = svg_text.lower()
    start = lowered.find("<svg")
    end = lowered.rfind("</svg>")
    if start < 0 or end < 0:
        return None

    body = svg_text[start : end + len("</svg>")]
    body = _XMLDECL_RE.sub("", body)
    body = _DOCTYPE_RE.sub("", body)
    body = _COMMENT_RE.sub("", body)
    body = _PAIRED_ACTIVE_RE.sub("", body)
    body = _LONE_ACTIVE_RE.sub("", body)
    body = _EVENT_ATTR_RE.sub(" ", body)
    body = _HREF_ATTR_RE.sub(_strip_remote_hrefs, body)

    root_end = body.find(">")
    if root_end > 0:
        root = body[: root_end + 1]
        if "viewbox" in root.lower():
            root = _ROOT_SIZE_RE.sub(" ", root)
            body = root + body[root_end + 1 :]
    return body.strip()


# Graphviz names every node and edge it lays out in a <title> child of the
# group it draws: <g id="node1" class="node"><title>g10</title>...  That is the
# only handle the report has on "which shape is which object", and it is enough
# to outline the ones a finding is scoped to -- without knowing anything about
# how the graph was built.
_NODE_TITLE_RE = re.compile(r"<g\b([^>]*)>(\s*<title>([^<]*)</title>)", re.IGNORECASE)

#: Class the report puts on the groups it highlights.
HIGHLIGHT_CLASS = "hal-viz-scope"


def highlight_svg(svg_text, labels):
    """Outline the graph nodes whose Graphviz ``<title>`` is in ``labels``.

    Returns ``(svg, matched)``.  ``matched`` is what makes the highlight
    honest: zero matches means the diagram and the finding do not talk about
    the same objects, and the report says so instead of showing an unmarked
    picture as if it were scoped.
    """
    labels = set(labels or ())
    if not svg_text or not labels:
        return svg_text, 0
    matched = [0]

    def replace(match):
        attributes = match.group(1)
        title = html.unescape(match.group(3))
        if title not in labels:
            return match.group(0)
        matched[0] += 1
        if 'class="' in attributes:
            attributes = attributes.replace('class="', 'class="' + HIGHLIGHT_CLASS + " ", 1)
        else:
            attributes = attributes + ' class="' + HIGHLIGHT_CLASS + '"'
        return "<g{}>{}".format(attributes, match.group(2))

    return _NODE_TITLE_RE.sub(replace, svg_text), matched[0]


def scope_labels(finding):
    """Graphviz node titles that plausibly denote this finding's objects.

    hal_viz names gate nodes ``g<id>`` and module nodes ``m<id>``; other
    exporters (the dataflow plugin, for instance) use the object names.  Both
    are offered, and a title that matches neither is simply not highlighted.
    """
    scope = finding.get("scope") or {}
    labels = set()
    for ref in scope.get("gates", []) or []:
        labels.add("g{}".format(ref.get("id")))
        if ref.get("name"):
            labels.add(ref["name"])
    for ref in scope.get("modules", []) or []:
        labels.add("m{}".format(ref.get("id")))
        if ref.get("name"):
            labels.add(ref["name"])
    for ref in scope.get("nets", []) or []:
        if ref.get("name"):
            labels.add(ref["name"])
    return labels


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def relative_href(target, base_dir):
    """Return a relative URL from ``base_dir`` to ``target``.

    Paths that cannot be expressed relatively (a different drive on Windows)
    fall back to an absolute ``file:`` URL, which still opens locally.
    """
    target = os.path.abspath(str(target))
    try:
        rel = os.path.relpath(target, os.path.abspath(str(base_dir)))
    except ValueError:
        rel = None
    if rel is None or os.path.isabs(rel):
        try:
            import pathlib

            return pathlib.Path(target).as_uri()
        except ValueError:  # pragma: no cover - as_uri only fails on relatives
            rel = target
    return _url_quote(rel.replace(os.sep, "/"), safe="/-._~()!$&*+,;=:@")


def resolve_path(raw, base_dir):
    """Resolve an evidence path (relative to its document unless absolute)."""
    raw = str(raw)
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(str(base_dir), raw))


# ---------------------------------------------------------------------------
# document loading
# ---------------------------------------------------------------------------


class LoadedDocument(object):
    """One findings document plus where it came from and how it validated."""

    def __init__(self, path, document, validation_errors=(), validated=False):
        self.path = os.path.abspath(str(path))
        self.directory = os.path.dirname(self.path)
        self.document = document
        self.validation_errors = list(validation_errors)
        self.validated = validated

    @property
    def findings(self):
        return self.document.get("findings", []) or []


def _import_findings():
    """Import ``hal_findings`` if it is reachable; return ``None`` otherwise.

    hal_viz and hal_findings are siblings in ``tools/``, so this normally just
    works.  When it does not, the report is still produced -- only the schema
    validation banner is replaced by an explicit "not validated" note.
    """
    try:
        from hal_findings import validate as findings_validate  # noqa: F401
    except ImportError:
        tools_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        try:
            from hal_findings import validate as findings_validate  # noqa: F401
        except ImportError:
            return None
    return findings_validate


def load_document(path, validate=True):
    """Read one findings document; validate it when ``hal_findings`` is around."""
    path = str(path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise ReportError("could not read findings document {}: {}".format(path, exc))
    except ValueError as exc:
        raise ReportError("{} is not valid JSON: {}".format(path, exc))
    if not isinstance(document, dict):
        raise ReportError(
            "{} does not contain a findings document (expected a JSON object, got "
            "{})".format(path, type(document).__name__)
        )

    errors, validated = [], False
    if validate:
        findings_validate = _import_findings()
        if findings_validate is not None:
            validated = True
            try:
                errors = findings_validate.collect_errors(document)
            except Exception as exc:  # noqa: BLE001 - report, never crash the run
                errors = ["validation failed: {}: {}".format(type(exc).__name__, exc)]
    return LoadedDocument(path, document, errors, validated)


def load_documents(paths, validate=True):
    return [load_document(path, validate=validate) for path in paths]


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------


class ReportOptions(object):
    """Everything the builder needs besides the documents themselves."""

    def __init__(
        self,
        report_path,
        title=None,
        embed=True,
        max_embed_bytes=4 * 1024 * 1024,
        max_items=25,
        render_dot="auto",
        copy_evidence=False,
        dot_binary=None,
        engine="dot",
        render_timeout=600,
        reporter=None,
    ):
        self.report_path = os.path.abspath(str(report_path))
        self.report_dir = os.path.dirname(self.report_path)
        self.title = title or "HAL findings report"
        self.embed = embed
        self.max_embed_bytes = max_embed_bytes
        self.max_items = max_items
        self.render_dot = render_dot
        self.copy_evidence = copy_evidence
        self.dot_binary = dot_binary
        self.engine = engine
        self.render_timeout = render_timeout
        self.reporter = reporter
        stem = os.path.splitext(os.path.basename(self.report_path))[0]
        self.evidence_dir = os.path.join(self.report_dir, stem + "_evidence")

    def info(self, message):
        if self.reporter is not None:
            self.reporter.info(message)

    def warn(self, message):
        if self.reporter is not None:
            self.reporter.warn(message)


# ---------------------------------------------------------------------------
# the builder
# ---------------------------------------------------------------------------

_STYLE = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{font:14px/1.55 system-ui,-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
 margin:0;padding:2rem 2.5rem;color:#16181d;background:#fff}
h1{font-size:1.5rem;margin:0 0 .25rem}
h2{font-size:1.15rem;margin:2.5rem 0 .75rem;border-bottom:1px solid #e3e5e9;padding-bottom:.3rem}
h3{font-size:1rem;margin:0 0 .35rem}
h4{font-size:.85rem;margin:1rem 0 .3rem;text-transform:uppercase;letter-spacing:.06em;color:#5b616e}
p{margin:.35rem 0}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.85em}
pre{background:#f6f7f9;border:1px solid #e3e5e9;border-radius:4px;padding:.6rem .7rem;overflow-x:auto}
a{color:#12509b}
table{border-collapse:collapse;margin:.4rem 0;font-size:.9em;width:100%}
th,td{border:1px solid #e3e5e9;padding:.25rem .5rem;text-align:left;vertical-align:top}
th{background:#f6f7f9;font-weight:600}
.subtle{color:#5b616e}
.wrap{overflow-wrap:anywhere}
.badge{display:inline-block;padding:.1rem .5rem;border-radius:999px;font-size:.78rem;
 font-weight:700;letter-spacing:.02em;border:2px solid;white-space:nowrap}
.badge .glyph{font-weight:700;margin-right:.3rem}
.badge.proven{background:#e3f5e8;border-color:#1f7a3f;color:#12522a;border-style:solid}
.badge.proven-bounded{background:#eaf4e6;border-color:#4b7d2f;color:#2f5220;border-style:dashed}
.badge.refuted{background:#fdE7e7;border-color:#a71d1d;color:#7d1414;border-style:solid}
.badge.refuted-bounded{background:#fdeee2;border-color:#b4531a;color:#823a11;border-style:dashed}
.badge.heuristic{background:#fff5d6;border-color:#9a7400;color:#6d5200;border-style:dotted}
.badge.unknown{background:#eceef2;border-color:#5b616e;color:#3c414c;border-style:dotted}
.badge.timeout{background:#f0e8fb;border-color:#6c3fb0;color:#4a2b79;border-style:dotted}
.badge.error{background:#f7dede;border-color:#6a0d0d;color:#500a0a;border-style:double}
.badge.unsupported{background:#e5eef2;border-color:#2b6478;color:#1e4553;border-style:double}
.badge.unrecognized{background:#fff;border-color:#000;color:#000;border-style:double}
.marker{display:inline-block;padding:.05rem .45rem;border-radius:3px;font-size:.78rem;
 font-weight:600;border:1px solid;margin:.1rem .25rem .1rem 0}
.marker.bounded{background:#fdeee2;border-color:#b4531a;color:#823a11}
.marker.unbounded{background:#e3f5e8;border-color:#1f7a3f;color:#12522a}
.marker.truncated{background:#fff5d6;border-color:#9a7400;color:#6d5200}
.marker.missing{background:#f7dede;border-color:#a71d1d;color:#7d1414}
.marker.info{background:#eceef2;border-color:#5b616e;color:#3c414c}
.marker.gap{background:#e5eef2;border-color:#2b6478;color:#1e4553}
.finding{border:1px solid #d8dbe1;border-left-width:6px;border-radius:6px;padding:1rem 1.1rem;
 margin:0 0 1.25rem;background:#fff}
.finding.proven{border-left-color:#1f7a3f}
.finding.proven-bounded{border-left-color:#4b7d2f}
.finding.refuted{border-left-color:#a71d1d}
.finding.refuted-bounded{border-left-color:#b4531a}
.finding.heuristic{border-left-color:#9a7400}
.finding.unknown{border-left-color:#5b616e}
.finding.timeout{border-left-color:#6c3fb0}
.finding.error{border-left-color:#6a0d0d}
.finding.unsupported{border-left-color:#2b6478}
.finding.unrecognized{border-left-color:#000}
.finding-head{display:flex;flex-wrap:wrap;gap:.5rem;align-items:baseline;justify-content:space-between}
.finding-id{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.8rem;color:#5b616e}
.claim{margin:.3rem 0 .6rem;font-size:.85rem;color:#3c414c}
.frame{border:1px solid #e3e5e9;border-radius:6px;background:#fafbfc;padding:.5rem;overflow:auto;
 max-height:36rem}
.frame svg,.frame img{max-width:100%;height:auto;display:block}
.hal-viz-scope ellipse,.hal-viz-scope polygon,.hal-viz-scope path{stroke:#a71d1d;stroke-width:2.5}
.hal-viz-scope text{font-weight:700;fill:#7d1414}
.legend{display:grid;grid-template-columns:repeat(auto-fill,minmax(21rem,1fr));gap:.4rem .9rem}
.legend div{display:flex;gap:.5rem;align-items:baseline}
.counts td:last-child{text-align:right;font-variant-numeric:tabular-nums}
.docmeta{background:#f6f7f9;border:1px solid #e3e5e9;border-radius:6px;padding:.7rem .9rem}
.banner{border:2px solid #a71d1d;background:#fdE7e7;border-radius:6px;padding:.7rem .9rem;margin:.8rem 0}
footer{margin-top:3rem;padding-top:.7rem;border-top:1px solid #e3e5e9;color:#5b616e;font-size:.82rem}
""".strip()


def _slug(value, fallback="x"):
    slug = re.sub(r"[^A-Za-z0-9_.:-]", "-", str(value or fallback))
    return slug.strip("-") or fallback


def _limited(items, limit):
    """Return ``(shown, hidden_count)``; ``limit`` of ``None`` shows everything."""
    items = list(items or [])
    if limit is None or limit < 0 or len(items) <= limit:
        return items, 0
    return items[:limit], len(items) - limit


class _Builder(object):
    def __init__(self, options):
        self.options = options
        self.parts = []
        self._copied = {}
        self._copy_index = 0
        self._dot_binary_checked = False
        self._dot_binary = None
        self.missing_evidence = 0
        self.embedded = 0

    # -- output helpers ----------------------------------------------------
    def add(self, markup):
        self.parts.append(markup)

    def html(self):
        return "\n".join(self.parts)

    # -- Graphviz ----------------------------------------------------------
    def dot_binary(self):
        if not self._dot_binary_checked:
            self._dot_binary_checked = True
            try:
                self._dot_binary = find_dot_binary(self.options.dot_binary)
            except RenderError as exc:
                self.options.warn(str(exc))
                self._dot_binary = None
        return self._dot_binary

    def render_dot_to_svg(self, dot_path):
        """Render a .dot to SVG text in a scratch directory; never writes next to it."""
        binary = self.dot_binary()
        if binary is None:
            return None, "Graphviz 'dot' was not found, so this graph is linked but not drawn"
        scratch = tempfile.mkdtemp(prefix="hal_viz_report_")
        try:
            out_path = os.path.join(scratch, "graph.svg")
            render_dot(
                dot_path,
                out_path,
                "svg",
                dot_binary=binary,
                engine=self.options.engine,
                timeout=self.options.render_timeout,
            )
            with open(out_path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read(), None
        except RenderError as exc:
            return None, "Graphviz could not render this graph: {}".format(exc)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    # -- evidence files ----------------------------------------------------
    def link_target(self, path):
        """Return the path the report should link to, copying it when asked."""
        path = os.path.abspath(path)
        if not self.options.copy_evidence or not os.path.isfile(path):
            return path
        if path in self._copied:
            return self._copied[path]
        os.makedirs(self.options.evidence_dir, exist_ok=True)
        name = "{:03d}_{}".format(self._copy_index, os.path.basename(path) or "evidence")
        self._copy_index += 1
        destination = os.path.join(self.options.evidence_dir, name)
        try:
            shutil.copyfile(path, destination)
        except OSError as exc:
            self.options.warn("could not copy {}: {}".format(path, exc))
            self._copied[path] = path
            return path
        self._copied[path] = destination
        return destination

    def file_link(self, path, label=None, download=True):
        """A relative link to a local file, or a visible marker when it is gone."""
        label = text(label or os.path.basename(path) or path)
        if not os.path.exists(path):
            self.missing_evidence += 1
            return (
                '<code class="wrap">{}</code> '
                '<span class="marker missing evidence-missing">artifact not found</span>'.format(
                    text(path)
                )
            )
        target = self.link_target(path)
        href = relative_href(target, self.options.report_dir)
        attrs = ' download' if download and os.path.isfile(target) else ""
        return '<a href="{}"{}>{}</a>'.format(href, attrs, label)

    def artifact_block(self, path, caption=None, highlight=None):
        """Link an artifact and, for a picture, embed it inline."""
        chunks = ["<p>{}</p>".format(self.file_link(path, caption))]
        if not os.path.isfile(path):
            return "\n".join(chunks)

        lower = path.lower()
        if lower.endswith(".svg"):
            chunks.append(self._embed_svg_file(path, highlight))
        elif lower.endswith(".dot") and self.options.render_dot != "never":
            chunks.append(self._embed_dot_file(path, highlight))
        elif lower.endswith((".png", ".gif", ".jpg", ".jpeg")):
            # Referenced, not inlined: a relative link to a local file needs no
            # network, and base64 would bloat the page.
            href = relative_href(self.link_target(path), self.options.report_dir)
            chunks.append(
                '<div class="frame"><img src="{}" alt="{}"></div>'.format(
                    href, text(caption or os.path.basename(path))
                )
            )
        return "\n".join(chunks)

    def _highlight(self, svg, highlight):
        """Outline the scope inside ``svg`` and describe what was matched."""
        if not highlight:
            return svg, ""
        svg, matched = highlight_svg(svg, highlight)
        if matched:
            return svg, (
                '<p class="subtle">{} node(s) of the finding scope are outlined in the '
                "diagram.</p>".format(matched)
            )
        return svg, (
            '<p><span class="marker truncated">this diagram carries no node named after an '
            "object in the finding scope, so nothing could be outlined; it may not be the "
            "scoped view of this finding</span></p>"
        )

    def _embed_svg_file(self, path, highlight=None):
        if not self.options.embed:
            return '<p class="subtle">embedding disabled (--no-embed)</p>'
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            return '<p><span class="marker missing">unreadable: {}</span></p>'.format(text(exc))
        if self.options.max_embed_bytes and size > self.options.max_embed_bytes:
            return (
                '<p><span class="marker truncated">not embedded: {} bytes exceeds the '
                "--max-embed-bytes limit of {}; open the linked file instead</span></p>".format(
                    size, self.options.max_embed_bytes
                )
            )
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                svg = sanitize_svg(handle.read())
        except OSError as exc:
            return '<p><span class="marker missing">unreadable: {}</span></p>'.format(text(exc))
        if not svg:
            return (
                '<p><span class="marker missing">not an SVG document, so it was linked '
                "but not drawn</span></p>"
            )
        self.embedded += 1
        svg, note = self._highlight(svg, highlight)
        return '<div class="frame">{}</div>{}'.format(svg, note)

    def _embed_dot_file(self, path, highlight=None):
        if not self.options.embed:
            return '<p class="subtle">embedding disabled (--no-embed)</p>'
        sibling = os.path.splitext(path)[0] + ".svg"
        if os.path.isfile(sibling) and self.options.render_dot != "always":
            return "\n".join(
                [
                    "<p>{}</p>".format(self.file_link(sibling, os.path.basename(sibling))),
                    self._embed_svg_file(sibling, highlight),
                ]
            )
        svg_text, problem = self.render_dot_to_svg(path)
        if svg_text is None:
            return '<p><span class="marker truncated">{}</span></p>'.format(text(problem))
        svg = sanitize_svg(svg_text)
        if not svg:
            return (
                '<p><span class="marker missing">Graphviz produced no usable SVG for this '
                "graph</span></p>"
            )
        self.embedded += 1
        svg, note = self._highlight(svg, highlight)
        return '<div class="frame">{}</div>{}'.format(svg, note)

    # -- document sections -------------------------------------------------
    def header(self, documents):
        options = self.options
        self.add("<h1>{}</h1>".format(text(options.title)))
        self.add(
            '<p class="subtle">{} document(s), {} finding(s). Generated by hal_viz {} at '
            "{}.</p>".format(
                len(documents),
                sum(len(doc.findings) for doc in documents),
                text(__version__),
                text(datetime.now().astimezone().isoformat(timespec="seconds")),
            )
        )
        self.add(
            '<p class="subtle">This page is self-contained: styles are inline, diagrams are '
            "inlined SVG and every other artifact is a relative link. It needs no HAL process "
            "and no network access.</p>"
        )

    def counts_table(self, documents):
        counts = {}
        for document in documents:
            for finding in document.findings:
                status = finding.get("status")
                counts[status] = counts.get(status, 0) + 1
        self.add("<h2>Result summary</h2>")
        if not counts:
            self.add("<p>These documents contain no findings.</p>")
        else:
            rows = ['<table class="counts"><tr><th>status</th><th>meaning</th><th>count</th></tr>']
            for status in sorted(counts, key=lambda s: status_meta(s)["sort"]):
                meta = status_meta(status)
                rows.append(
                    "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                        self.badge(status), text(meta["claim"]), counts[status]
                    )
                )
            rows.append("</table>")
            self.add("\n".join(rows))
        self.add(
            '<p class="subtle">A bounded result is never folded into a proof and an absent '
            "result is never silence: unsupported, unknown, timeout and error findings are "
            "listed exactly like the rest.</p>"
        )

    def legend(self):
        self.add("<h2>Status legend</h2>")
        entries = [
            "<div>{}<span class=\"subtle\">{}</span></div>".format(
                self.badge(status), text(status_meta(status)["claim"])
            )
            for status in STATUS_ORDER
        ]
        self.add('<div class="legend">{}</div>'.format("".join(entries)))

    def badge(self, status):
        meta = status_meta(status)
        return (
            '<span class="badge {css}" title="{title}">'
            '<span class="glyph">{glyph}</span>{label}</span>'.format(
                css=meta["css"],
                title=text("{}: {}".format(status, meta["claim"])),
                glyph=text(meta["glyph"]),
                label=text(meta["label"]),
            )
        )

    def index(self, documents):
        self.add("<h2>Findings index</h2>")
        rows = ['<table><tr><th>status</th><th>finding</th><th>document</th></tr>']
        empty = True
        for doc_index, document in enumerate(documents):
            for finding in self.sorted_findings(document):
                empty = False
                anchor = self.anchor(doc_index, finding)
                rows.append(
                    '<tr><td>{}</td><td><a href="#{}">{}</a><br><span class="finding-id">{}</span>'
                    "</td><td>{}</td></tr>".format(
                        self.badge(finding.get("status")),
                        text(anchor),
                        text(finding.get("title") or finding.get("id")),
                        text(finding.get("id")),
                        text(os.path.basename(document.path)),
                    )
                )
        rows.append("</table>")
        if empty:
            self.add("<p>No findings to index.</p>")
        else:
            self.add("\n".join(rows))

    @staticmethod
    def anchor(doc_index, finding):
        return "f{}-{}".format(doc_index, _slug(finding.get("id"), "finding"))

    @staticmethod
    def sorted_findings(document):
        return sorted(
            document.findings,
            key=lambda f: (status_meta(f.get("status"))["sort"], str(f.get("id", ""))),
        )

    def document_section(self, doc_index, document):
        data = document.document
        self.add(
            '<h2 id="doc{}">Document: {}</h2>'.format(doc_index, text(os.path.basename(document.path)))
        )

        if document.validation_errors:
            items = "".join(
                "<li>{}</li>".format(text(error)) for error in document.validation_errors[:50]
            )
            more = ""
            if len(document.validation_errors) > 50:
                more = '<p><span class="marker truncated">{} further problem(s) not shown</span></p>'.format(
                    len(document.validation_errors) - 50
                )
            self.add(
                '<div class="banner"><strong>This document does not validate against the '
                "hal_findings schema.</strong> Read every claim below with that in mind."
                "<ul>{}</ul>{}</div>".format(items, more)
            )
        elif not document.validated:
            self.add(
                '<p><span class="marker info">not validated: the hal_findings package was not '
                "importable, so the schema was not checked</span></p>"
            )

        producer = data.get("producer", {}) or {}
        analysis = data.get("analysis", {}) or {}
        plugin = analysis.get("plugin", {}) or {}
        hal = analysis.get("hal", {}) or {}
        rows = [
            ("path", "<code class=\"wrap\">{}</code>".format(text(document.path))),
            ("schema version", text(data.get("schema_version", "(missing)"))),
            ("generated at", text(data.get("generated_at", "(not recorded)"))),
            (
                "producer",
                text("{} {}".format(producer.get("name", "?"), producer.get("version", "?"))),
            ),
            (
                "analysis",
                text(
                    "{} {}{}".format(
                        plugin.get("name", "?"),
                        plugin.get("version", "?"),
                        " via " + analysis.get("entry_point", "")
                        if analysis.get("entry_point")
                        else "",
                    )
                ),
            ),
        ]
        if hal:
            rows.append(
                ("HAL", text("{} {}".format(hal.get("version", "?"), hal.get("commit", ""))))
            )
        if producer.get("command"):
            rows.append(
                ("command", "<code class=\"wrap\">{}</code>".format(text(" ".join(producer["command"]))))
            )
        if analysis.get("duration_s") is not None:
            rows.append(("duration", text("{} s".format(analysis["duration_s"]))))
        self.add(
            '<div class="docmeta"><table>{}</table></div>'.format(
                "".join("<tr><th>{}</th><td>{}</td></tr>".format(text(k), v) for k, v in rows)
            )
        )

        if analysis.get("configuration"):
            self.add("<h4>Analysis configuration</h4>")
            self.add("<pre>{}</pre>".format(_json_text(analysis["configuration"])))

        self.artifacts_table(data.get("artifacts", []) or [])

        for note in data.get("notes", []) or []:
            self.add('<p class="subtle">Note: {}</p>'.format(text(note)))

        findings = self.sorted_findings(document)
        if not findings:
            self.add("<p>This document reports no findings.</p>")
        for finding in findings:
            self.finding_section(doc_index, document, finding)

    def artifacts_table(self, artifacts):
        self.add("<h4>Artifacts these findings are scoped to</h4>")
        if not artifacts:
            self.add(
                '<p><span class="marker missing">no artifacts declared; gate and net IDs below '
                "cannot be resolved</span></p>"
            )
            return
        rows = [
            "<table><tr><th>artifact_id</th><th>kind</th><th>path</th><th>content hash</th>"
            "<th>size</th></tr>"
        ]
        for artifact in artifacts:
            if artifact.get("sha256"):
                digest = '<code title="{}">{}</code>'.format(
                    text(artifact["sha256"]), text(artifact["sha256"][:16] + "…")
                )
            else:
                digest = (
                    '<span class="marker missing">unhashed: {}</span>'.format(
                        text(artifact.get("unhashed_reason", "no reason recorded"))
                    )
                )
            counts = []
            if artifact.get("gate_count") is not None:
                counts.append("{} gates".format(artifact["gate_count"]))
            if artifact.get("net_count") is not None:
                counts.append("{} nets".format(artifact["net_count"]))
            rows.append(
                "<tr><td><code>{}</code></td><td>{}</td><td class=\"wrap\">{}</td><td>{}</td>"
                "<td>{}</td></tr>".format(
                    text(artifact.get("artifact_id")),
                    text(artifact.get("kind")),
                    text(artifact.get("path", "")),
                    digest,
                    text(", ".join(counts)),
                )
            )
        rows.append("</table>")
        self.add("\n".join(rows))

    # -- one finding -------------------------------------------------------
    def finding_section(self, doc_index, document, finding):
        status = finding.get("status")
        meta = status_meta(status)
        self.add(
            '<section class="finding {css}" id="{anchor}">'.format(
                css=meta["css"], anchor=text(self.anchor(doc_index, finding))
            )
        )
        self.add(
            '<div class="finding-head"><h3>{}</h3>{}</div>'.format(
                text(finding.get("title") or finding.get("id")), self.badge(status)
            )
        )
        self.add('<p class="finding-id">{}</p>'.format(text(finding.get("id"))))
        self.add('<p class="claim">{}</p>'.format(text(meta["claim"])))

        self.add(self.markers(finding))

        if finding.get("summary"):
            self.add("<p>{}</p>".format(text(finding["summary"])))

        highlight = scope_labels(finding)
        self.method_block(finding)
        self.assumptions_block(finding)
        self.scope_block(document, finding)
        self.result_blocks(document, finding, highlight)
        self.evidence_block(
            document, finding.get("evidence"), "Evidence", highlight=highlight
        )

        for key, heading in (("metrics", "Metrics"), ("data", "Analysis data")):
            if finding.get(key):
                self.add("<h4>{}</h4>".format(text(heading)))
                self.add("<pre>{}</pre>".format(_json_text(finding[key])))

        if finding.get("tags"):
            self.add(
                '<p class="subtle">tags: {}</p>'.format(
                    ", ".join(text(tag) for tag in finding["tags"])
                )
            )
        self.add("</section>")

    def markers(self, finding):
        """The chips that must never be lost: bounds, severity, confidence, gaps."""
        chips = []
        bounds = finding.get("bounds")
        if bounds is not None:
            if bounds.get("unbounded"):
                chips.append('<span class="marker unbounded">unbounded claim</span>')
            else:
                detail = []
                if bounds.get("cycle_bound") is not None:
                    detail.append("{} cycle(s)".format(bounds["cycle_bound"]))
                if bounds.get("unroll_depth") is not None:
                    detail.append("unroll depth {}".format(bounds["unroll_depth"]))
                if bounds.get("input_bound") is not None:
                    detail.append("{} input(s)".format(bounds["input_bound"]))
                chips.append(
                    '<span class="marker bounded">BOUNDED: only up to {}</span>'.format(
                        text(", ".join(detail) or "an unstated bound")
                    )
                )
            if bounds.get("description"):
                chips.append(
                    '<span class="marker info">{}</span>'.format(text(bounds["description"]))
                )
        if (finding.get("method") or {}).get("bounded") and (
            bounds is None or bounds.get("unbounded")
        ):
            chips.append(
                '<span class="marker bounded">method is bounded</span>'
            )
        if finding.get("severity"):
            chips.append(
                '<span class="marker info">severity: {}</span>'.format(text(finding["severity"]))
            )
        if finding.get("confidence") is not None:
            chips.append(
                '<span class="marker info">confidence: {}</span>'.format(
                    text(finding["confidence"])
                )
            )
        if finding.get("unsupported"):
            chips.append('<span class="marker gap">coverage gap</span>')
        limits = finding.get("limits") or {}
        if limits.get("hit"):
            chips.append('<span class="marker truncated">a resource limit was reached</span>')
        return "<p>{}</p>".format("".join(chips)) if chips else ""

    def method_block(self, finding):
        method = finding.get("method") or {}
        if not method:
            self.add(
                '<p><span class="marker missing">no method recorded; the claim cannot be '
                "judged</span></p>"
            )
            return
        self.add("<h4>Method</h4>")
        self.add(
            "<p>{} <span class=\"subtle\">(kind: {}, {})</span></p>".format(
                text(method.get("name")),
                text(method.get("kind")),
                "bounded" if method.get("bounded") else "unbounded",
            )
        )
        if method.get("description"):
            self.add('<p class="subtle">{}</p>'.format(text(method["description"])))
        if method.get("parameters"):
            self.add("<pre>{}</pre>".format(_json_text(method["parameters"])))
        solver = finding.get("solver")
        if solver:
            self.add(
                '<p class="subtle">solver: {} {} &mdash; {}</p>'.format(
                    text(solver.get("name")),
                    text(solver.get("version", "")),
                    text(
                        ", ".join(
                            "{}: {}".format(key, _scalar(solver[key]))
                            for key in ("queries", "sat", "unsat", "unknown", "wall_time_s")
                            if solver.get(key) is not None
                        )
                    ),
                )
            )
        limits = finding.get("limits")
        if limits:
            self.add(
                '<p class="subtle">limits: {}</p>'.format(
                    text(
                        ", ".join(
                            "{}: {}".format(key, _scalar(value))
                            for key, value in sorted(limits.items())
                            if key != "description"
                        )
                    )
                )
            )
            if limits.get("description"):
                self.add('<p class="subtle">{}</p>'.format(text(limits["description"])))

    def assumptions_block(self, finding):
        assumptions = finding.get("assumptions")
        if assumptions is None:
            return
        self.add("<h4>Assumptions</h4>")
        if not assumptions:
            self.add(
                '<p><span class="marker info">none recorded: this claim is stated to rest on '
                "nothing</span></p>"
            )
            return
        rows = ["<table><tr><th>id</th><th>kind</th><th>assumption</th><th>checked?</th></tr>"]
        shown, hidden = _limited(assumptions, self.options.max_items)
        for assumption in shown:
            if assumption.get("discharged") is True:
                checked = "checked by this run"
            elif assumption.get("discharged") is False:
                checked = '<span class="marker missing">assumed, not checked</span>'
            else:
                checked = '<span class="marker info">not stated</span>'
            rows.append(
                "<tr><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    text(assumption.get("id")),
                    text(assumption.get("kind", "")),
                    text(assumption.get("description")),
                    checked,
                )
            )
        rows.append("</table>")
        self.add("\n".join(rows))
        if hidden:
            self.add(self.truncation_marker(hidden, "assumption"))
        for assumption in shown:
            if assumption.get("evidence"):
                self.evidence_block(
                    None,
                    assumption["evidence"],
                    "Evidence for assumption {}".format(assumption.get("id")),
                    base_dir=self._current_dir,
                )

    def truncation_marker(self, hidden, noun):
        return (
            '<p><span class="marker truncated">TRUNCATED: {} further {}(s) not shown; raise '
            "--max-items to see them</span></p>".format(hidden, text(noun))
        )

    def scope_block(self, document, finding):
        scope = finding.get("scope") or {}
        self.add("<h4>Scope</h4>")
        if scope.get("description"):
            self.add("<p>{}</p>".format(text(scope["description"])))
        self.add(
            '<p class="subtle">artifacts: {}</p>'.format(
                ", ".join("<code>{}</code>".format(text(a)) for a in scope.get("artifact_ids", []))
                or "none"
            )
        )
        if scope.get("gate_types"):
            self.add(
                '<p class="subtle">gate types: {}</p>'.format(
                    ", ".join("<code>{}</code>".format(text(t)) for t in scope["gate_types"])
                )
            )
        for key, noun, columns in (
            ("gates", "gate", ("id", "name", "type", "module")),
            ("nets", "net", ("id", "name", "role")),
            ("modules", "module", ("id", "name", "type")),
        ):
            refs = scope.get(key)
            if not refs:
                continue
            shown, hidden = _limited(refs, self.options.max_items)
            rows = [
                "<table><tr><th>artifact</th>"
                + "".join("<th>{}</th>".format(text(column)) for column in columns)
                + "</tr>"
            ]
            for ref in shown:
                cells = []
                for column in columns:
                    value = ref.get(column)
                    if column == "module" and isinstance(value, dict):
                        value = "{} (id {})".format(value.get("name", ""), value.get("id", ""))
                    cells.append("<td class=\"wrap\">{}</td>".format(text(value if value is not None else "")))
                rows.append(
                    "<tr><td><code>{}</code></td>{}</tr>".format(
                        text(ref.get("artifact_id")), "".join(cells)
                    )
                )
            rows.append("</table>")
            self.add("<p class=\"subtle\">{} {}(s) in scope</p>".format(len(refs), text(noun)))
            self.add("\n".join(rows))
            if hidden:
                self.add(self.truncation_marker(hidden, noun))

    def result_blocks(self, document, finding, highlight=None):
        counterexample = finding.get("counterexample")
        if counterexample:
            self.add("<h4>Counterexample</h4>")
            self.add("<p>{}</p>".format(text(counterexample.get("description"))))
            if counterexample.get("cycle_bound") is not None:
                self.add(
                    '<p><span class="marker bounded">witness found at cycle bound {}</span></p>'.format(
                        text(counterexample["cycle_bound"])
                    )
                )
            witness = counterexample.get("witness") or []
            if witness:
                shown, hidden = _limited(witness, self.options.max_items)
                rows = ["<table><tr><th>cycle</th><th>signal</th><th>value</th><th>net</th></tr>"]
                for entry in shown:
                    net = entry.get("net") or {}
                    rows.append(
                        "<tr><td>{}</td><td class=\"wrap\">{}</td><td><code>{}</code></td>"
                        "<td class=\"wrap\">{}</td></tr>".format(
                            text(entry.get("cycle", "")),
                            text(entry.get("signal")),
                            text(entry.get("value")),
                            text(
                                "{} (id {})".format(net.get("name", ""), net.get("id", ""))
                                if net
                                else ""
                            ),
                        )
                    )
                rows.append("</table>")
                self.add("\n".join(rows))
                if hidden:
                    self.add(self.truncation_marker(hidden, "witness entry"))
            elif counterexample.get("witness_available") is False:
                self.add(
                    '<p><span class="marker missing">no witness is available for this '
                    "refutation</span></p>"
                )
            else:
                self.add(
                    '<p><span class="marker info">no witness assignments were recorded in the '
                    "document</span></p>"
                )
            self.evidence_block(
                document,
                counterexample.get("evidence"),
                "Counterexample evidence",
                highlight=highlight,
            )

        unsupported = finding.get("unsupported")
        if unsupported:
            self.add("<h4>Not covered by this analysis</h4>")
            self.add(
                '<p><span class="marker gap">{}</span> {}</p>'.format(
                    text(unsupported.get("kind")), text(unsupported.get("reason"))
                )
            )
            primitives = unsupported.get("primitives") or []
            if primitives:
                shown, hidden = _limited(primitives, self.options.max_items)
                rows = [
                    "<table><tr><th>gate type</th><th>count</th><th>properties</th>"
                    "<th>why it is not covered</th></tr>"
                ]
                for primitive in shown:
                    rows.append(
                        "<tr><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                            text(primitive.get("gate_type")),
                            text(primitive.get("count", "")),
                            text(", ".join(primitive.get("properties", []) or [])),
                            text(primitive.get("reason")),
                        )
                    )
                rows.append("</table>")
                self.add("\n".join(rows))
                if hidden:
                    self.add(self.truncation_marker(hidden, "unsupported primitive"))
                examples = [
                    ref for primitive in shown for ref in primitive.get("example_gates", []) or []
                ]
                if examples:
                    shown_examples, hidden_examples = _limited(examples, self.options.max_items)
                    self.add(
                        '<p class="subtle">example gates: {}</p>'.format(
                            ", ".join(
                                "<code>{}</code> (id {})".format(
                                    text(ref.get("name")), text(ref.get("id"))
                                )
                                for ref in shown_examples
                            )
                        )
                    )
                    if hidden_examples:
                        self.add(self.truncation_marker(hidden_examples, "example gate"))

        error = finding.get("error")
        if error:
            self.add("<h4>Analysis failure</h4>")
            self.add(
                "<p><code>{}</code>: {}</p>".format(
                    text(error.get("kind")), text(error.get("message"))
                )
            )
            if error.get("detail"):
                self.add("<pre>{}</pre>".format(text(error["detail"])))

    def evidence_block(self, document, evidence_list, heading, base_dir=None, highlight=None):
        evidence_list = evidence_list or []
        if not evidence_list:
            return
        base_dir = base_dir or (document.directory if document else self._current_dir)
        self.add("<h4>{}</h4>".format(text(heading)))
        for item in evidence_list:
            kind = item.get("kind", "file")
            description = item.get("description") or ""
            self.add(
                '<p><span class="marker info">{}</span>{}</p>'.format(
                    text(kind), " " + text(description) if description else ""
                )
            )
            if item.get("command"):
                self.add(
                    "<pre>$ {}</pre>".format(text(" ".join(str(c) for c in item["command"])))
                )
            if "inline" in item and item.get("inline") is not None:
                self.add("<pre>{}</pre>".format(_json_text(item["inline"])))
            if item.get("path"):
                resolved = resolve_path(item["path"], base_dir)
                self.add(self.artifact_block(resolved, os.path.basename(resolved), highlight))
                if item.get("sha256"):
                    self.add(
                        '<p class="subtle">sha256 <code>{}</code></p>'.format(text(item["sha256"]))
                    )

    # -- extra hal_viz artifacts ------------------------------------------
    def extra_artifacts(self, artifacts):
        if not artifacts:
            return
        self.add("<h2>hal_viz artifacts</h2>")
        self.add(
            '<p class="subtle">Diagrams passed in with --artifact. They are not attached to a '
            "particular finding.</p>"
        )
        for path in artifacts:
            resolved = os.path.abspath(str(path))
            self.add("<h3>{}</h3>".format(text(os.path.basename(resolved))))
            self.add(self.artifact_block(resolved, os.path.basename(resolved)))

    def footer(self):
        self.add(
            "<footer>Generated by hal_viz {} &mdash; findings follow the hal_findings schema "
            "(tools/hal_findings). Diagrams are produced by the hal_viz netlist_graph / "
            "module_tree / dataflow / clock_tree commands and referenced as evidence; this "
            "report never re-derives a graph. Waveform and trace evidence is linked for "
            "download, not rendered: see tools/hal_viz/README.md.</footer>".format(
                text(__version__)
            )
        )

    _current_dir = None

    def build(self, documents, extra_artifacts=()):
        self.header(documents)
        self.counts_table(documents)
        self.legend()
        self.index(documents)
        for doc_index, document in enumerate(documents):
            self._current_dir = document.directory
            self.document_section(doc_index, document)
        self._current_dir = self.options.report_dir
        self.extra_artifacts(list(extra_artifacts))
        self.footer()
        return self.html()


def build_report(documents, options, extra_artifacts=()):
    """Render ``documents`` (a list of :class:`LoadedDocument`) to an HTML page."""
    builder = _Builder(options)
    body = builder.build(documents, extra_artifacts)
    page = "\n".join(
        [
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>{}</title>".format(text(options.title)),
            "<style>{}</style>".format(_STYLE),
            "</head><body>",
            body,
            "</body></html>",
            "",
        ]
    )
    return page, builder


def write_report(documents, options, extra_artifacts=()):
    """Build the report and write it to ``options.report_path``."""
    page, builder = build_report(documents, options, extra_artifacts)
    parent = os.path.dirname(options.report_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        with open(options.report_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(page)
    except OSError as exc:
        raise ReportError("could not write {}: {}".format(options.report_path, exc))
    return options.report_path, builder
