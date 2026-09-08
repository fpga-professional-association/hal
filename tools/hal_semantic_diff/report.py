"""A dependency-free, evidence-linked HTML report over findings documents.

The report exists to make one thing hard to get wrong: reading a result as
stronger than it is.  So the status is the loudest element on every card, a
proof always shows the assumptions it rests on, a counterexample always shows
its witness (or says it has none), and every piece of evidence is a link to the
file the run actually produced -- with the changed-cone diagrams inlined, since
those are the whole point of localizing a change.

``hal_viz`` is expected to grow a ``report`` subcommand (issue #18).  When that
lands, :func:`render` here becomes redundant for the generic case and
:func:`hal_viz_report_available` lets the CLI prefer it; until then this is the
report generator for ``hal_semantic_diff`` output.  Both consume the same
``hal_findings`` document, so nothing downstream changes when the switch
happens.
"""

import html
import json
import os

__all__ = [
    "STATUS_LABELS",
    "hal_viz_report_available",
    "render",
    "write_report",
]

#: ``status -> (human label, css class)``.  The wording is deliberately blunt.
STATUS_LABELS = {
    "proven_under_assumptions": ("proven (under assumptions)", "ok"),
    "proven_bounded": ("proven up to a cycle bound", "bounded"),
    "counterexample": ("refuted -- counterexample", "bad"),
    "bounded_counterexample": ("refuted within a cycle bound", "bad"),
    "heuristic": ("heuristic -- not a proof", "warn"),
    "unknown": ("no verdict", "warn"),
    "timeout": ("aborted at a resource limit", "warn"),
    "error": ("the analysis failed", "warn"),
    "unsupported": ("outside the analysis' coverage", "warn"),
}

_ORDER = [
    "counterexample",
    "bounded_counterexample",
    "error",
    "timeout",
    "unknown",
    "unsupported",
    "heuristic",
    "proven_bounded",
    "proven_under_assumptions",
]

_CSS = """
:root { color-scheme: light; }
body { font: 15px/1.55 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
       margin: 0; background: #f6f7f9; color: #1d2025; }
main { max-width: 1080px; margin: 0 auto; padding: 24px 20px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 32px 0 10px; border-bottom: 1px solid #d8dce2; padding-bottom: 6px; }
h3 { font-size: 15px; margin: 0 0 6px; }
.sub { color: #5b6470; margin: 0 0 18px; font-size: 13px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; background: #fff; }
th, td { border: 1px solid #dde1e7; padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #eef1f5; font-weight: 600; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
pre { background: #23262b; color: #e6e9ee; padding: 10px 12px; overflow-x: auto;
      font-size: 12px; border-radius: 5px; }
.card { background: #fff; border: 1px solid #dde1e7; border-left-width: 5px;
        border-radius: 5px; padding: 14px 16px; margin: 12px 0; }
.card.ok { border-left-color: #2f855a; }
.card.bad { border-left-color: #c0392b; }
.card.warn { border-left-color: #b7791f; }
.card.bounded { border-left-color: #2b6cb0; }
.badge { display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: .04em;
         text-transform: uppercase; padding: 2px 8px; border-radius: 10px; color: #fff; }
.badge.ok { background: #2f855a; } .badge.bad { background: #c0392b; }
.badge.warn { background: #b7791f; } .badge.bounded { background: #2b6cb0; }
.counts { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 4px; padding: 0; list-style: none; }
.counts li { background: #fff; border: 1px solid #dde1e7; border-radius: 5px; padding: 8px 12px; }
.counts .n { font-size: 20px; font-weight: 700; display: block; }
details { margin-top: 8px; } summary { cursor: pointer; color: #34506e; font-size: 13px; }
.evidence a { color: #2b6cb0; }
.diagram { background: #fff; border: 1px solid #dde1e7; border-radius: 5px; padding: 8px;
           margin-top: 10px; overflow-x: auto; }
.diagram svg { max-width: 100%; height: auto; }
.meta { font-size: 12px; color: #5b6470; }
ul.tight { margin: 6px 0; padding-left: 20px; }
"""


def hal_viz_report_available():
    """True when ``hal_viz`` already ships a ``report`` subcommand.

    Feature detection, not a version check: this package must keep working
    against a checkout where issue #18 has landed and one where it has not,
    without editing ``hal_viz``.
    """
    try:
        from hal_viz import cli as viz_cli
    except ImportError:
        return False
    return hasattr(viz_cli, "cmd_report")


def _esc(value):
    return html.escape(str(value), quote=True)


def _status_class(status):
    return STATUS_LABELS.get(status, (status, "warn"))[1]


def _status_label(status):
    return STATUS_LABELS.get(status, (status, "warn"))[0]


def _artifact_table(document):
    rows = []
    for artifact in document.get("artifacts", []):
        pin = artifact.get("sha256")
        pin_text = (
            "<code>{}</code>".format(_esc(pin[:16] + "..."))
            if pin
            else "<em>unhashed: {}</em>".format(_esc(artifact.get("unhashed_reason", "")))
        )
        rows.append(
            "<tr><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{}</td></tr>".format(
                _esc(artifact.get("artifact_id", "")),
                _esc(artifact.get("kind", "")),
                _esc(artifact.get("path", "") or ""),
                pin_text,
                _esc(
                    "{} gates / {} nets".format(
                        artifact.get("gate_count", "?"), artifact.get("net_count", "?")
                    )
                    if artifact.get("gate_count") is not None
                    else ""
                ),
            )
        )
    return (
        "<table><tr><th>artifact</th><th>kind</th><th>path</th><th>sha256</th>"
        "<th>size</th></tr>{}</table>".format("".join(rows))
    )


def _counts(document):
    counts = {}
    for finding in document.get("findings", []):
        status = finding.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    items = []
    for status in _ORDER:
        if status not in counts:
            continue
        items.append(
            '<li class="{}"><span class="n">{}</span>{}</li>'.format(
                _status_class(status), counts[status], _esc(_status_label(status))
            )
        )
    return '<ul class="counts">{}</ul>'.format("".join(items))


def _witness_table(counterexample):
    witness = counterexample.get("witness") or []
    if not witness:
        return (
            "<p class='meta'><strong>No witness.</strong> "
            "<code>witness_available</code> is <code>{}</code>; the difference is "
            "reported without an input assignment.</p>".format(
                _esc(counterexample.get("witness_available"))
            )
        )
    rows = "".join(
        "<tr><td><code>{}</code></td><td><code>{}</code></td></tr>".format(
            _esc(entry.get("signal", "")), _esc(entry.get("value", ""))
        )
        for entry in witness
    )
    return (
        "<p class='meta'>Input assignment that makes the two builds differ:</p>"
        "<table><tr><th>variable</th><th>value</th></tr>{}</table>".format(rows)
    )


def _inline_svg(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ""
    index = text.find("<svg")
    if index < 0:
        return ""
    return '<div class="diagram">{}</div>'.format(text[index:])


def _evidence_block(finding, base_dir, inline_svg):
    entries = list(finding.get("evidence") or [])
    for extra in (finding.get("counterexample") or {}).get("evidence") or []:
        entries.append(extra)
    if not entries:
        return ""
    items = []
    diagrams = []
    for entry in entries:
        path = entry.get("path")
        description = entry.get("description") or entry.get("kind", "evidence")
        if path:
            items.append(
                '<li class="evidence"><a href="{}">{}</a> &mdash; {}</li>'.format(
                    _esc(path), _esc(os.path.basename(path)), _esc(description)
                )
            )
            resolved = path if os.path.isabs(path) else os.path.join(base_dir or ".", path)
            if inline_svg and resolved.lower().endswith(".svg") and os.path.isfile(resolved):
                diagrams.append(_inline_svg(resolved))
        elif entry.get("command"):
            items.append(
                "<li class='evidence'><code>{}</code> &mdash; {}</li>".format(
                    _esc(" ".join(entry["command"])), _esc(description)
                )
            )
        else:
            items.append(
                "<li class='evidence'>{} &mdash; inline</li>".format(_esc(description))
            )
    return "<ul class='tight'>{}</ul>{}".format("".join(items), "".join(diagrams))


def _assumptions_block(finding):
    assumptions = finding.get("assumptions")
    if not assumptions:
        return ""
    items = "".join(
        "<li><code>{}</code> ({}): {}</li>".format(
            _esc(entry.get("id", "")),
            _esc(entry.get("kind", "unspecified")),
            _esc(entry.get("description", "")),
        )
        for entry in assumptions
    )
    return (
        "<details><summary>{} assumption(s) this rests on</summary>"
        "<ul class='tight'>{}</ul></details>".format(len(assumptions), items)
    )


def _unsupported_block(finding):
    unsupported = finding.get("unsupported")
    if not unsupported:
        return ""
    primitives = unsupported.get("primitives") or []
    rows = "".join(
        "<tr><td><code>{}</code></td><td>{}</td><td>{}</td></tr>".format(
            _esc(entry.get("gate_type", "")),
            _esc(entry.get("count", "")),
            _esc(entry.get("reason", "")),
        )
        for entry in primitives
    )
    table = (
        "<table><tr><th>gate type</th><th>count</th><th>reason</th></tr>{}</table>".format(rows)
        if rows
        else ""
    )
    return "<p class='meta'><strong>Coverage gap ({}):</strong> {}</p>{}".format(
        _esc(unsupported.get("kind", "")), _esc(unsupported.get("reason", "")), table
    )


def _finding_card(finding, base_dir, inline_svg):
    status = finding.get("status", "unknown")
    css = _status_class(status)
    parts = [
        '<div class="card {}">'.format(css),
        '<span class="badge {}">{}</span>'.format(css, _esc(_status_label(status))),
        "<h3 style='margin-top:8px'>{}</h3>".format(_esc(finding.get("title", ""))),
        "<p class='meta'><code>{}</code></p>".format(_esc(finding.get("id", ""))),
    ]
    if finding.get("summary"):
        parts.append("<p>{}</p>".format(_esc(finding["summary"])))

    method = finding.get("method") or {}
    bounds = finding.get("bounds") or {}
    bound_text = (
        "unbounded"
        if bounds.get("unbounded")
        else "bounded (cycle_bound={})".format(bounds.get("cycle_bound"))
        if bounds
        else "no bounds recorded"
    )
    parts.append(
        "<p class='meta'>method: <strong>{}</strong> ({}) &middot; claim: {}</p>".format(
            _esc(method.get("name", "")), _esc(method.get("kind", "")), _esc(bound_text)
        )
    )

    if finding.get("counterexample"):
        parts.append(_witness_table(finding["counterexample"]))
    if finding.get("error"):
        parts.append(
            "<p class='meta'><strong>Error ({}):</strong> {}</p>".format(
                _esc(finding["error"].get("kind", "")), _esc(finding["error"].get("message", ""))
            )
        )
    parts.append(_unsupported_block(finding))
    parts.append(_assumptions_block(finding))
    parts.append(_evidence_block(finding, base_dir, inline_svg))

    if finding.get("data"):
        parts.append(
            "<details><summary>raw data</summary><pre>{}</pre></details>".format(
                _esc(json.dumps(finding["data"], indent=2, sort_keys=True))
            )
        )
    parts.append("</div>")
    return "".join(parts)


def render(document, title=None, base_dir=None, inline_svg=True):
    """Render one findings document to a self-contained HTML string."""
    analysis = document.get("analysis", {})
    plugin = analysis.get("plugin", {})
    heading = title or "{} report".format(plugin.get("name", "analysis"))

    findings = list(document.get("findings", []))
    order = {status: index for index, status in enumerate(_ORDER)}
    findings.sort(
        key=lambda finding: (
            0 if finding.get("id", "").endswith("/summary") else 1,
            order.get(finding.get("status"), len(order)),
            finding.get("id", ""),
        )
    )

    notes = document.get("notes") or []
    notes_html = (
        "<ul class='tight'>{}</ul>".format(
            "".join("<li>{}</li>".format(_esc(note)) for note in notes)
        )
        if notes
        else ""
    )

    body = [
        "<main>",
        "<h1>{}</h1>".format(_esc(heading)),
        "<p class='sub'>{} {} &middot; generated {} &middot; entry point <code>{}</code></p>".format(
            _esc(plugin.get("name", "")),
            _esc(plugin.get("version", "")),
            _esc(document.get("generated_at", "unknown")),
            _esc(analysis.get("entry_point", "")),
        ),
        _counts(document),
        "<h2>Inputs</h2>",
        _artifact_table(document),
    ]
    if notes_html:
        body.append("<h2>How to read this</h2>")
        body.append(notes_html)
    body.append("<h2>Findings</h2>")
    for finding in findings:
        body.append(_finding_card(finding, base_dir, inline_svg))
    body.append("</main>")

    return (
        "<!DOCTYPE html>\n<html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>{}</title><style>{}</style></head><body>{}</body></html>\n".format(
            _esc(heading), _CSS, "".join(body)
        )
    )


def write_report(document, path, title=None, inline_svg=True):
    """Write the HTML report next to its evidence; returns ``path``."""
    directory = os.path.dirname(os.path.abspath(str(path))) or "."
    text = render(document, title=title, base_dir=directory, inline_svg=inline_svg)
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path
