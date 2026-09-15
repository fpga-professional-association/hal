"""Drive the Graphviz ``dot`` binary and emit a small HTML index.

Rendering is always optional: hal_viz writes the ``.dot`` file first and only
then tries to render it, so a machine without Graphviz still gets a usable,
machine-renderable artifact.
"""

import html
import os
import shutil
import subprocess

__all__ = [
    "RenderError",
    "RENDER_FORMATS",
    "LAYOUT_ENGINES",
    "find_dot_binary",
    "render_dot",
    "write_html_index",
    "write_html_page",
    "inline_svg",
]

# Formats we are willing to ask Graphviz for.  "none" means "write .dot only".
RENDER_FORMATS = ("svg", "png", "pdf", "none")

# Selected through ``dot -K<engine>`` so only the single ``dot`` binary is
# required, regardless of which layout the user picks.
LAYOUT_ENGINES = ("dot", "neato", "fdp", "sfdp", "circo", "twopi", "osage")

_ENV_DOT = "HAL_VIZ_DOT"


class RenderError(RuntimeError):
    """Raised when Graphviz is present but fails to render a graph."""


def find_dot_binary(explicit=None):
    """Locate the Graphviz ``dot`` executable, or return ``None``.

    Search order: explicit argument, ``$HAL_VIZ_DOT``, then ``PATH``.
    """
    for candidate in (explicit, os.environ.get(_ENV_DOT)):
        if not candidate:
            continue
        resolved = shutil.which(candidate) or (
            candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else None
        )
        if resolved:
            return resolved
        raise RenderError(
            "Graphviz executable {!r} was requested but is not usable.".format(candidate)
        )
    return shutil.which("dot")


def render_dot(dot_path, out_path, fmt, dot_binary=None, engine="dot", timeout=600):
    """Render ``dot_path`` to ``out_path`` in ``fmt``; return the output path.

    ``dot_binary`` may be a path already resolved by :func:`find_dot_binary`.
    Raises :class:`RenderError` on any failure, including a missing binary, so
    callers can decide whether that is fatal.
    """
    if fmt == "none":
        return None
    if fmt not in RENDER_FORMATS:
        raise RenderError(
            "unsupported output format {!r} (expected one of: {})".format(
                fmt, ", ".join(RENDER_FORMATS)
            )
        )
    if engine not in LAYOUT_ENGINES:
        raise RenderError(
            "unsupported layout engine {!r} (expected one of: {})".format(
                engine, ", ".join(LAYOUT_ENGINES)
            )
        )

    binary = dot_binary or find_dot_binary()
    if not binary:
        raise RenderError(
            "the Graphviz 'dot' executable was not found on PATH. Install Graphviz "
            "(https://graphviz.org/download/), set $HAL_VIZ_DOT to its full path, or "
            "pass --format none and render the emitted .dot file elsewhere."
        )

    command = [binary, "-K" + engine, "-T" + fmt, "-o", str(out_path), str(dot_path)]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RenderError(
            "Graphviz timed out after {}s rendering {}. The graph is probably too "
            "large; narrow the scope (--module/--gate/--depth) or use "
            "--engine sfdp.".format(timeout, dot_path)
        )
    except OSError as exc:
        raise RenderError("could not execute {}: {}".format(binary, exc))

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise RenderError(
            "Graphviz exited with status {} while rendering {}{}".format(
                completed.returncode,
                dot_path,
                ":\n" + detail if detail else ".",
            )
        )
    return out_path


_PAGE_CSS = """\
:root{color-scheme:light}
*{box-sizing:border-box}
body{font:14px/1.55 system-ui,-apple-system,Segoe UI,Helvetica,sans-serif;
margin:0;padding:2rem;color:#1b1b1b;background:#fff}
main{max-width:100%;margin:0 auto}
h1{font-size:1.35rem;margin:0 0 .2rem}
p.caption{color:#444;margin:.2rem 0 1rem}
ul.stats{list-style:none;display:flex;flex-wrap:wrap;gap:.4rem;padding:0;margin:0 0 1rem}
ul.stats li{border:1px solid #e0e0e0;border-radius:999px;padding:.1rem .7rem;
background:#fafafa;color:#333;font-size:.85rem}
ul.stats li b{font-weight:600}
p.warning{border-left:4px solid #c0392b;background:#fdecea;margin:0 0 1rem;
padding:.5rem .75rem;color:#7d241a}
.figure{border:1px solid #ddd;border-radius:6px;background:#fff;padding:.5rem;
overflow:auto}
.figure svg{max-width:100%;height:auto;display:block}
.legend{margin:1.25rem 0 0;border:1px solid #e0e0e0;border-radius:6px;
padding:.75rem 1rem;background:#fcfcfc}
.legend h2{font-size:.95rem;margin:0 0 .5rem}
.legend dl{display:grid;grid-template-columns:max-content 1fr;gap:.3rem .9rem;margin:0}
.legend dt{font-weight:600;color:#2a5d8f}
.legend dd{margin:0;color:#444}
footer{margin-top:1.5rem;color:#777;font-size:.8rem}
a{color:#1a73e8}
"""


def inline_svg(path):
    """Return the ``<svg>`` element of ``path`` ready to be inlined in HTML.

    The XML declaration, the DOCTYPE and Graphviz's leading comment are dropped
    -- an SVG document embedded in HTML must start at its root element.
    """
    with open(str(path), encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    start = text.find("<svg")
    if start < 0:
        raise RenderError("{} does not contain an <svg> element".format(path))
    return text[start:]


def write_html_page(
    path,
    title,
    svg_path=None,
    caption=None,
    stats=(),
    legend=(),
    warnings=(),
    fallback=None,
    footer=None,
):
    """Write one standalone HTML page around a single inlined diagram.

    Unlike :func:`write_html_index`, which links the files it lists, this
    inlines the SVG into the page, so the result is a single file that opens
    offline: inline CSS, no scripts, no request to anything outside it.
    ``stats`` is a sequence of ``(label, value)`` pairs shown as a caption row,
    ``legend`` a sequence of ``(term, meaning)`` pairs, and ``fallback`` a path
    linked when there is no SVG to inline (no Graphviz, ``--format none``).
    """
    path = str(path)
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>{}</title>".format(html.escape(title)),
        "<style>",
        _PAGE_CSS.rstrip(),
        "</style></head><body><main>",
        "<h1>{}</h1>".format(html.escape(title)),
    ]
    if caption:
        parts.append('<p class="caption">{}</p>'.format(html.escape(str(caption))))
    if stats:
        parts.append('<ul class="stats">')
        for label, value in stats:
            parts.append(
                "<li><b>{}</b> {}</li>".format(
                    html.escape(str(value)), html.escape(str(label))
                )
            )
        parts.append("</ul>")
    for warning in warnings:
        parts.append('<p class="warning">{}</p>'.format(html.escape(str(warning))))

    parts.append('<div class="figure">')
    if svg_path and os.path.isfile(str(svg_path)):
        parts.append(inline_svg(svg_path))
    else:
        name = os.path.basename(str(fallback)) if fallback else None
        parts.append(
            "<p>No SVG was inlined"
            + (
                ' &mdash; the diagram is in <a href="{0}">{0}</a>, next to this'
                " page.".format(html.escape(name, quote=True))
                if name
                else " (nothing was rendered; ask for <code>--format svg</code>)."
            )
            + "</p>"
        )
    parts.append("</div>")

    if legend:
        parts.append('<section class="legend"><h2>Legend</h2><dl>')
        for term, meaning in legend:
            parts.append(
                "<dt>{}</dt><dd>{}</dd>".format(
                    html.escape(str(term)), html.escape(str(meaning))
                )
            )
        parts.append("</dl></section>")
    if footer:
        parts.append("<footer>{}</footer>".format(html.escape(str(footer))))
    parts.append("</main></body></html>")

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(parts) + "\n")
    return path


def write_html_index(path, title, entries, notes=()):
    """Write a dependency-free HTML index linking/inlining generated files.

    ``entries`` is a sequence of ``(caption, filename)`` pairs; filenames are
    interpreted relative to the directory containing ``path``.
    """
    path = str(path)
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>{}</title>".format(html.escape(title)),
        "<style>",
        "body{font:14px/1.5 system-ui,-apple-system,Segoe UI,Helvetica,sans-serif;"
        "margin:0;padding:2rem;color:#1b1b1b;background:#fff}",
        "h1{font-size:1.4rem;margin:0 0 .25rem}",
        "p.note{color:#555;margin:.25rem 0 1.5rem}",
        "section{margin:0 0 2.5rem}",
        "h2{font-size:1.05rem;margin:0 0 .5rem}",
        ".frame{border:1px solid #ddd;border-radius:6px;overflow:auto;"
        "background:#fafafa;padding:.5rem}",
        ".frame img,.frame object{max-width:100%;display:block}",
        "a{color:#1a73e8}",
        "</style></head><body>",
        "<h1>{}</h1>".format(html.escape(title)),
    ]
    for note in notes:
        parts.append('<p class="note">{}</p>'.format(html.escape(str(note))))

    if not entries:
        parts.append("<p>No output files were produced.</p>")

    for caption, filename in entries:
        name = os.path.basename(str(filename))
        safe = html.escape(name, quote=True)
        parts.append("<section>")
        parts.append(
            '<h2>{} &mdash; <a href="{}">{}</a></h2>'.format(
                html.escape(str(caption)), safe, safe
            )
        )
        lower = name.lower()
        if lower.endswith(".svg"):
            parts.append(
                '<div class="frame"><object type="image/svg+xml" data="{}">'
                '<a href="{}">{}</a></object></div>'.format(safe, safe, safe)
            )
        elif lower.endswith(".png"):
            parts.append(
                '<div class="frame"><img src="{}" alt="{}"></div>'.format(safe, safe)
            )
        parts.append("</section>")

    parts.append("</body></html>")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(parts) + "\n")
    return path
