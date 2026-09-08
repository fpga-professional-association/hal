"""Dependency-free Graphviz DOT emission.

This module is pure stdlib on purpose: it contains all of the formatting and
escaping logic, so it can be unit tested without a built HAL and without
Graphviz installed.  Nothing in here imports ``hal_py``.
"""

import re

__all__ = [
    "escape",
    "quote",
    "truncate",
    "sanitize_id",
    "DotGraph",
    "DotSubgraph",
]

# Graphviz attribute names are plain identifiers; anything else is rejected
# rather than silently emitted, because an unquoted bogus key would produce a
# .dot file that fails to parse much later, at render time.
_ATTR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")

_UNSAFE_ID_RE = re.compile(r"[^A-Za-z0-9_]")


def escape(value):
    """Escape ``value`` for use inside a DOT double-quoted string.

    Backslashes and double quotes are escaped, all flavours of line break are
    folded into the DOT ``\\n`` line-break escape (which Graphviz renders as a
    centred new line in a label), tabs become spaces and any other control
    character is replaced by a space so the resulting file stays parseable.
    """
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    text = text.replace("\t", " ")
    return "".join(ch if ch >= " " or ch == "\\" else " " for ch in text)


def quote(value):
    """Return ``value`` as a complete DOT double-quoted string literal."""
    return '"' + escape(value) + '"'


def truncate(value, limit=48, ellipsis="..."):
    """Shorten ``value`` to at most ``limit`` characters for use in a label."""
    text = value if isinstance(value, str) else str(value)
    if limit is None or limit <= 0 or len(text) <= limit:
        return text
    if limit <= len(ellipsis):
        return text[:limit]
    return text[: limit - len(ellipsis)] + ellipsis


def sanitize_id(value, prefix="n"):
    """Turn ``value`` into a conservative DOT node identifier.

    Node identifiers are always quoted when emitted, so this is only about
    keeping generated files readable and diffable.
    """
    text = _UNSAFE_ID_RE.sub("_", value if isinstance(value, str) else str(value))
    if not text or not (text[0].isalpha() or text[0] == "_"):
        text = prefix + "_" + text
    return text


def _format_attrs(attrs):
    """Render an attribute mapping as ``[k="v", ...]`` (empty string if none)."""
    if not attrs:
        return ""
    parts = []
    for key, val in attrs.items():
        if val is None:
            continue
        if not _ATTR_NAME_RE.match(key):
            raise ValueError("invalid DOT attribute name: {!r}".format(key))
        parts.append("{}={}".format(key, quote(val)))
    if not parts:
        return ""
    return " [" + ", ".join(parts) + "]"


class _Container(object):
    """Shared node/edge/subgraph bookkeeping for graphs and subgraphs."""

    def __init__(self):
        self.graph_attrs = {}
        self.node_defaults = {}
        self.edge_defaults = {}
        self._nodes = {}
        self._edges = []
        self._subgraphs = []

    # -- construction ----------------------------------------------------

    def add_node(self, node_id, label=None, **attrs):
        """Add (or update) a node.  Returns the node id for convenience."""
        node_id = str(node_id)
        if label is not None:
            attrs["label"] = label
        existing = self._nodes.get(node_id)
        if existing is None:
            self._nodes[node_id] = dict(attrs)
        else:
            existing.update(attrs)
        return node_id

    def has_node(self, node_id):
        return str(node_id) in self._nodes

    def add_edge(self, source, target, label=None, **attrs):
        """Add an edge.  Parallel edges are allowed and preserved."""
        if label is not None:
            attrs["label"] = label
        self._edges.append((str(source), str(target), dict(attrs)))

    def add_subgraph(self, name, label=None, cluster=False, **graph_attrs):
        """Add a nested subgraph.  Use ``cluster=True`` for a boxed cluster."""
        sub = DotSubgraph(name, cluster=cluster)
        if label is not None:
            sub.graph_attrs["label"] = label
        sub.graph_attrs.update(graph_attrs)
        self._subgraphs.append(sub)
        return sub

    def add_cluster(self, name, label=None, **graph_attrs):
        """Shorthand for :meth:`add_subgraph` with ``cluster=True``."""
        return self.add_subgraph(name, label=label, cluster=True, **graph_attrs)

    # -- inspection ------------------------------------------------------

    @property
    def node_count(self):
        return len(self._nodes) + sum(s.node_count for s in self._subgraphs)

    @property
    def edge_count(self):
        return len(self._edges) + sum(s.edge_count for s in self._subgraphs)

    # -- emission --------------------------------------------------------

    def _body_lines(self, indent, edge_op):
        pad = " " * indent
        lines = []
        for keyword, attrs in (
            ("graph", self.graph_attrs),
            ("node", self.node_defaults),
            ("edge", self.edge_defaults),
        ):
            rendered = _format_attrs(attrs)
            if rendered:
                lines.append("{}{}{};".format(pad, keyword, rendered))
        if lines:
            lines.append("")

        for node_id, attrs in self._nodes.items():
            lines.append("{}{}{};".format(pad, quote(node_id), _format_attrs(attrs)))
        if self._nodes:
            lines.append("")

        for sub in self._subgraphs:
            lines.extend(sub._lines(indent, edge_op))
            lines.append("")

        for source, target, attrs in self._edges:
            lines.append(
                "{}{} {} {}{};".format(
                    pad, quote(source), edge_op, quote(target), _format_attrs(attrs)
                )
            )

        while lines and lines[-1] == "":
            lines.pop()
        return lines


class DotSubgraph(_Container):
    """A nested ``subgraph`` block, optionally a ``cluster_*`` box."""

    def __init__(self, name, cluster=False):
        _Container.__init__(self)
        name = sanitize_id(name, prefix="sg")
        if cluster and not name.startswith("cluster"):
            name = "cluster_" + name
        self.name = name
        self.cluster = cluster

    def _lines(self, indent, edge_op):
        pad = " " * indent
        lines = ["{}subgraph {} {{".format(pad, quote(self.name))]
        lines.extend(self._body_lines(indent + 2, edge_op))
        lines.append("{}}}".format(pad))
        return lines


class DotGraph(_Container):
    """A top-level DOT graph."""

    def __init__(self, name="G", directed=True, strict=False, comment=None):
        _Container.__init__(self)
        self.name = name
        self.directed = directed
        self.strict = strict
        self.comment = comment

    def to_dot(self):
        """Serialize the graph to a DOT source string (ends with a newline)."""
        keyword = "digraph" if self.directed else "graph"
        edge_op = "->" if self.directed else "--"
        header = "{}{} {} {{".format(
            "strict " if self.strict else "", keyword, quote(self.name)
        )

        lines = []
        if self.comment:
            for chunk in str(self.comment).splitlines() or [""]:
                lines.append("// " + chunk)
        lines.append(header)
        lines.extend(self._body_lines(2, edge_op))
        lines.append("}")
        return "\n".join(lines) + "\n"

    def write(self, path, encoding="utf-8"):
        """Write the DOT source to ``path`` and return ``path``."""
        with open(str(path), "w", encoding=encoding, newline="\n") as handle:
            handle.write(self.to_dot())
        return path
