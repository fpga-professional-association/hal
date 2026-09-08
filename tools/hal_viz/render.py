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
