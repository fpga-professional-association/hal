#!/usr/bin/env python3
"""Stamp out a working HAL analysis plugin from the templates in ``tools/plugin_template``.

What you get is not a stub. The generated plugin builds, is registered with CMake
and ctest, exposes a Python module, declares its capabilities in a schema-valid
``capabilities.json``, and ships a driver that turns its result into a
``hal_findings`` document. It also *does* something -- it groups sequential gates
by the net driving their clock pin -- so that "did I break it?" has an answer
from the first build onwards. Replace the analysis; keep the wiring.

::

    python3 tools/new_plugin.py my_analysis                       # into plugins/
    python3 tools/new_plugin.py my_analysis --out-dir /tmp/scratch
    python3 tools/new_plugin.py my_analysis --description "..." --force

The generated tree::

    plugins/<name>/
      CMakeLists.txt                     build flag, capability embedding, test registration
      capabilities.json                  what the plugin needs, per tools/hal_capabilities
      README.md
      include/<name>/<name>.h            the analysis API
      include/<name>/plugin_<name>.h     the plugin interface
      src/<name>.cpp
      src/plugin_<name>.cpp
      python/python_bindings.cpp         the hal_plugins.<name> module
      python/run_<name>.py               driver emitting a hal_findings document
      test/CMakeLists.txt
      test/<name>.cpp                    gtest suite

No CMake edit is needed: ``plugins/CMakeLists.txt`` adds every subdirectory that
has a ``CMakeLists.txt`` of its own. Build it with ``-DPL_<NAME>=ON`` (or
``-DBUILD_ALL_PLUGINS=ON``).

Generation is deterministic: the same name and description always produce
byte-identical files, which is what lets ``tools/test_new_plugin.py`` assert that
``plugins/example_analysis`` is still exactly what the templates produce.

Whether you want a C++ plugin at all is a real question -- see
``documentation/plugin_development.md``. A netlist walk over the ``hal_py``
bindings is usually the right first answer.
"""

import argparse
import os
import sys

__all__ = [
    "TEMPLATE_DIRECTORY",
    "FILES",
    "DEFAULT_DESCRIPTION",
    "render",
    "generated_files",
    "create_plugin",
    "unignore",
    "main",
]

TEMPLATE_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin_template")

DEFAULT_DESCRIPTION = "Group sequential gates by the net that drives their clock pin."

#: ``(template file, generated path)``; the path is rendered like the contents.
FILES = (
    ("CMakeLists.txt.in", "CMakeLists.txt"),
    ("capabilities.json.in", "capabilities.json"),
    ("README.md.in", "README.md"),
    ("analysis_header.h.in", os.path.join("include", "##LOWER##", "##LOWER##.h")),
    ("plugin_header.h.in", os.path.join("include", "##LOWER##", "plugin_##LOWER##.h")),
    ("analysis_source.cpp.in", os.path.join("src", "##LOWER##.cpp")),
    ("plugin_source.cpp.in", os.path.join("src", "plugin_##LOWER##.cpp")),
    ("python_bindings.cpp.in", os.path.join("python", "python_bindings.cpp")),
    ("run_plugin.py.in", os.path.join("python", "run_##LOWER##.py")),
    ("test_CMakeLists.txt.in", os.path.join("test", "CMakeLists.txt")),
    ("test_source.cpp.in", os.path.join("test", "##LOWER##.cpp")),
)

#: Files that must be executable in the generated tree.
EXECUTABLE = (os.path.join("python", "run_##LOWER##.py"),)

#: C++ keywords a plugin name must not collide with; the name becomes a namespace.
_RESERVED = frozenset(
    [
        "alignas", "alignof", "and", "asm", "auto", "bool", "break", "case", "catch", "char",
        "class", "const", "continue", "default", "delete", "do", "double", "else", "enum",
        "explicit", "export", "extern", "false", "float", "for", "friend", "goto", "if",
        "inline", "int", "long", "mutable", "namespace", "new", "not", "operator", "or",
        "private", "protected", "public", "register", "return", "short", "signed", "sizeof",
        "static", "struct", "switch", "template", "this", "throw", "true", "try", "typedef",
        "typename", "union", "unsigned", "using", "virtual", "void", "volatile", "while", "xor",
        "hal", "test",
    ]
)


class PluginNameError(ValueError):
    """The requested plugin name cannot be used."""


def check_name(name):
    """Raise :class:`PluginNameError` unless ``name`` works as a C++ identifier and a directory."""
    if not name:
        raise PluginNameError("plugin name must not be empty")
    if name != name.lower():
        raise PluginNameError(
            "plugin name {!r} must be lower case: it is used verbatim as the C++ namespace, "
            "the library file name and the Python module name".format(name)
        )
    if not name.replace("_", "").isalnum() or not name.replace("_", "").isascii():
        raise PluginNameError(
            "plugin name {!r} must consist of ASCII letters, digits and underscores".format(name)
        )
    if name[0] == "_" or name[0].isdigit():
        raise PluginNameError(
            "plugin name {!r} must start with a letter (it becomes a C++ identifier)".format(name)
        )
    if name in _RESERVED:
        raise PluginNameError(
            "plugin name {!r} is a C++ keyword or would shadow an existing namespace".format(name)
        )
    return name


def class_name(name):
    """``my_analysis`` -> ``MyAnalysis``."""
    return "".join(part[:1].upper() + part[1:] for part in name.split("_"))


def render(text, name, description):
    """Substitute the template placeholders in ``text``."""
    return (
        text.replace("##CLASSNAME##", class_name(name))
        .replace("##UPPER##", name.upper())
        .replace("##LOWER##", name)
        .replace("##DESCRIPTION##", description)
    )


def _read_template(file_name):
    path = os.path.join(TEMPLATE_DIRECTORY, file_name)
    if not os.path.isfile(path):
        raise IOError(
            "template {!r} is missing from {}. The scaffold is incomplete; do not hand-write "
            "the missing file, restore it.".format(file_name, TEMPLATE_DIRECTORY)
        )
    with open(path, "r", newline="") as handle:
        return handle.read()


def generated_files(name, description=DEFAULT_DESCRIPTION):
    """``{relative path: contents}`` for the plugin that would be generated.

    Kept separate from writing so the tests can compare a checked-in plugin
    against the templates without touching the file system.
    """
    check_name(name)
    files = {}
    for template_name, target in FILES:
        rendered_target = render(target, name, description).replace(os.sep, "/")
        files[rendered_target] = render(_read_template(template_name), name, description)
    return files


def _gitignore_lines(name):
    return ["!{}*".format(name), "!{}/**/*".format(name)]


def unignore(out_dir, name):
    """Allow-list ``name`` in ``out_dir/.gitignore``, if that file is an allow list.

    ``plugins/.gitignore`` ignores ``*`` and re-includes each plugin by name. A
    generated plugin is therefore invisible to ``git status`` until it is listed
    -- a trap worth closing automatically rather than documenting.

    Returns the path if it was changed, ``None`` if there was nothing to do.
    """
    path = os.path.join(out_dir, ".gitignore")
    if not os.path.isfile(path):
        return None
    with open(path, "r", newline="") as handle:
        contents = handle.read()
    lines = contents.splitlines()
    if not lines or lines[0].strip() != "*":
        return None    # not an allow list; leave it alone
    wanted = _gitignore_lines(name)
    if all(line in lines for line in wanted):
        return None

    # Keep the file's alphabetical order: insert before the first entry that sorts
    # after this one, else before the trailing non-plugin entries.
    insert_at = len(lines)
    for index, line in enumerate(lines):
        if line.startswith("!") and line.endswith("*") and "/" not in line:
            if line[1:-1] > name:
                insert_at = index
                break
        elif line.startswith("!") and not line.endswith("*"):
            insert_at = index
            break
    lines[insert_at:insert_at] = [entry for entry in wanted if entry not in lines]
    with open(path, "w", newline="") as handle:
        handle.write("\n".join(lines) + ("\n" if contents.endswith("\n") else ""))
    return path


def create_plugin(name, out_dir, description=DEFAULT_DESCRIPTION, force=False):
    """Write the generated plugin into ``out_dir/name``; returns the written paths."""
    check_name(name)
    directory = os.path.join(out_dir, name)
    if os.path.exists(directory) and not force:
        raise IOError(
            "{} already exists. Pass --force to overwrite it, or pick another name.".format(
                directory
            )
        )

    written = []
    for relative, contents in sorted(generated_files(name, description).items()):
        path = os.path.join(directory, *relative.split("/"))
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        # Newlines are written verbatim so that a plugin generated on Windows is
        # byte-identical to one generated on Linux; the templates are LF.
        with open(path, "w", newline="") as handle:
            handle.write(contents)
        written.append(path)

    for relative in EXECUTABLE:
        path = os.path.join(directory, *render(relative, name, description).split(os.sep))
        if os.path.isfile(path):
            os.chmod(path, os.stat(path).st_mode | 0o111)

    return written


def build_parser():
    parser = argparse.ArgumentParser(
        prog="tools/new_plugin.py",
        description="Generate a working HAL analysis plugin.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("name", help="plugin name: lower case, a valid C++ identifier")
    parser.add_argument(
        "--out-dir",
        default=None,
        help="directory to create the plugin in (default: the repository's plugins/)",
    )
    parser.add_argument(
        "--description",
        default=DEFAULT_DESCRIPTION,
        help="one-line description, used in the plugin, its bindings and its capabilities",
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing directory"
    )
    parser.add_argument(
        "--no-gitignore",
        action="store_true",
        help="do not allow-list the new plugin in plugins/.gitignore",
    )
    return parser


def main(argv=None, out=None, err=None):
    args = build_parser().parse_args(argv)
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    out_dir = args.out_dir
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(TEMPLATE_DIRECTORY), os.pardir, "plugins")
        out_dir = os.path.normpath(out_dir)

    if '"' in args.description or "\n" in args.description:
        err.write(
            "the description is embedded in C++ string literals and in JSON; it must not "
            "contain a double quote or a newline.\n"
        )
        return 1

    try:
        written = create_plugin(args.name, out_dir, args.description, force=args.force)
    except (PluginNameError, IOError, OSError) as exc:
        err.write("{}\n".format(exc))
        return 1

    directory = os.path.join(out_dir, args.name)
    out.write("created {} ({} files)\n".format(directory, len(written)))

    if not args.no_gitignore:
        changed = unignore(out_dir, args.name)
        if changed:
            out.write("allow-listed {} in {}\n".format(args.name, changed))
    out.write(
        "\nNext:\n"
        "  1. cmake .. -DPL_{upper}=ON -DBUILD_TESTS=ON && cmake --build . --target {name}\n"
        "  2. ctest -R runTest-{name} --output-on-failure\n"
        "  3. HAL_PY_PATH=<build>/lib python3 {dir}/python/run_{name}.py <netlist> "
        "--output findings.json\n"
        "  4. python3 tools/hal_capabilities check {name} --netlist <netlist>\n"
        "\nThen replace the analysis in src/{name}.cpp and its capabilities.json.\n"
        "Walkthrough: documentation/plugin_development.md\n".format(
            upper=args.name.upper(), name=args.name, dir=directory.replace(os.sep, "/")
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
