"""The bitstream families HAL knows about, and how to open each of them.

Everything family-specific lives here: the magic bytes that identify a file, the chain of external
programs that turns it into Verilog, the gate library the result has to be read with, and what to
tell the user when the toolchain is missing. Adding a family -- or correcting a converter's command
line -- is an edit to this file or a call to :func:`register` from outside; nothing else in
hal_bitstream knows what an iCE40 is.

Status values, printed by ``hal_bitstream families``:

``verified``
    the chain is exercised end to end in this fork (a real bitstream, built by the open toolchain,
    converted and loaded into ``hal_py``).
``unverified``
    the chain is implemented from the converter's own documentation but has not been run here,
    because the toolchain is not installed in this fork's containers. It is expected to work; if it
    does not, this file is the one place to fix.
``no-converter``
    the format is documented by an open project, but no open converter produces a netlist from it
    yet. Detection still works, and the error says exactly that instead of pretending otherwise.
"""

import os

__all__ = [
    "Magic",
    "ConverterStep",
    "Family",
    "UnknownFamilyError",
    "families",
    "get",
    "register",
    "families_for_extension",
    "gate_library_path",
    "STATUS_VERIFIED",
    "STATUS_UNVERIFIED",
    "STATUS_NO_CONVERTER",
]

STATUS_VERIFIED = "verified"
STATUS_UNVERIFIED = "unverified"
STATUS_NO_CONVERTER = "no-converter"

#: Gate libraries are referenced repository-relative so a checkout is enough to resolve them.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GATE_LIBRARY_DIR = os.path.join("plugins", "gate_libraries", "definitions")


class UnknownFamilyError(KeyError):
    """Raised for a family key that is not registered."""


class Magic(object):
    """A byte pattern that identifies a format, plus what it means in prose."""

    def __init__(self, pattern, description, window=4096, offset=None):
        self.pattern = pattern
        self.description = description
        self.window = window
        self.offset = offset

    def matches(self, head):
        if self.offset is not None:
            return head[self.offset : self.offset + len(self.pattern)] == self.pattern
        return self.pattern in head[: self.window]

    def evidence(self, head):
        """Human-readable 'why this file is that format'."""
        if self.offset is not None:
            return "{} at offset {}".format(self.description, self.offset)
        return "{} at offset {}".format(self.description, head[: self.window].find(self.pattern))


class ConverterStep(object):
    """One external program in a family's bitstream -> Verilog chain.

    ``arguments`` is a template: ``{input}`` and ``{output}`` are substituted with the paths of
    this step's input and output. When ``capture_stdout`` is set the program's standard output *is*
    the output file (icebox_vlog writes the netlist to stdout).
    """

    def __init__(
        self,
        program,
        arguments=("{input}",),
        output_suffix=".out",
        capture_stdout=False,
        input_suffixes=None,
        package=None,
        url=None,
        purpose="",
        version_argument="--help",
    ):
        self.program = program
        self.arguments = tuple(arguments)
        self.output_suffix = output_suffix
        self.capture_stdout = capture_stdout
        #: Only run when the current file has one of these suffixes (``None``: always run). This is
        #: what lets a chain accept both a packed bitstream and an already-unpacked ASCII file.
        self.input_suffixes = tuple(s.lower() for s in input_suffixes) if input_suffixes else None
        self.package = package
        self.url = url
        self.purpose = purpose
        self.version_argument = version_argument

    def applies_to(self, path):
        if self.input_suffixes is None:
            return True
        return os.path.splitext(str(path))[1].lower() in self.input_suffixes

    def command(self, program_path, input_path, output_path):
        arguments = [
            argument.format(input=str(input_path), output=str(output_path))
            for argument in self.arguments
        ]
        return [str(program_path)] + arguments

    def install_hint(self):
        parts = []
        if self.package:
            parts.append("Debian/Ubuntu: 'sudo apt-get install {}'".format(self.package))
        if self.url:
            parts.append("source: {}".format(self.url))
        return "; ".join(parts)

    def to_json(self):
        return {
            "program": self.program,
            "arguments": list(self.arguments),
            "capture_stdout": self.capture_stdout,
            "package": self.package,
            "url": self.url,
            "purpose": self.purpose,
        }


class Family(object):
    """A bitstream family: how to recognise it, how to open it, how to read the result."""

    def __init__(
        self,
        key,
        name,
        vendor,
        toolchain,
        extensions=(),
        magic=(),
        steps=(),
        gate_library=None,
        status=STATUS_UNVERIFIED,
        notes="",
        documentation=None,
    ):
        self.key = key
        self.name = name
        self.vendor = vendor
        self.toolchain = toolchain
        self.extensions = tuple(e.lower() for e in extensions)
        self.magic = tuple(magic)
        self.steps = tuple(steps)
        self.gate_library = gate_library
        self.status = status
        self.notes = notes
        self.documentation = documentation

    @property
    def has_converter(self):
        return bool(self.steps)

    def gate_library_file(self):
        """Absolute path of the gate library, or ``None`` when the family has none."""
        if not self.gate_library:
            return None
        return os.path.join(REPO_ROOT, GATE_LIBRARY_DIR, self.gate_library)

    def to_json(self):
        return {
            "key": self.key,
            "name": self.name,
            "vendor": self.vendor,
            "toolchain": self.toolchain,
            "extensions": list(self.extensions),
            "status": self.status,
            "gate_library": self.gate_library,
            "steps": [step.to_json() for step in self.steps],
            "notes": self.notes,
            "documentation": self.documentation,
        }


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

_REGISTRY = {}
_ORDER = []


def register(family, replace=False):
    """Add (or replace) a family. The extension point for out-of-tree converters."""
    if not isinstance(family, Family):
        raise TypeError("register() takes a Family, got {!r}".format(type(family).__name__))
    if family.key in _REGISTRY and not replace:
        raise ValueError(
            "a family with key {!r} is already registered; pass replace=True to override "
            "it".format(family.key)
        )
    if family.key not in _REGISTRY:
        _ORDER.append(family.key)
    _REGISTRY[family.key] = family
    return family


def families():
    """Every registered family, in registration order."""
    return tuple(_REGISTRY[key] for key in _ORDER)


def get(key):
    try:
        return _REGISTRY[key]
    except KeyError:
        raise UnknownFamilyError(
            "unknown bitstream family {!r}; known families: {}".format(
                key, ", ".join(sorted(_REGISTRY))
            )
        )


def families_for_extension(extension):
    extension = extension.lower()
    return tuple(family for family in families() if extension in family.extensions)


def gate_library_path(key):
    return get(key).gate_library_file()


# ---------------------------------------------------------------------------
# built-in families
# ---------------------------------------------------------------------------

register(
    Family(
        key="ice40",
        name="Lattice iCE40",
        vendor="Lattice",
        toolchain="Project IceStorm",
        extensions=(".bin", ".asc"),
        magic=(
            Magic(b"\x7e\xaa\x99\x7e", "the iCE40 sync word 7E AA 99 7E"),
            Magic(b".device 1k", "an icebox ASCII header (.device 1k)", window=512),
            Magic(b".device 5k", "an icebox ASCII header (.device 5k)", window=512),
            Magic(b".device 8k", "an icebox ASCII header (.device 8k)", window=512),
            Magic(b".device 384", "an icebox ASCII header (.device 384)", window=512),
            Magic(b".device u4k", "an icebox ASCII header (.device u4k)", window=512),
            Magic(b".device lm4k", "an icebox ASCII header (.device lm4k)", window=512),
        ),
        steps=(
            ConverterStep(
                program="iceunpack",
                arguments=("{input}", "{output}"),
                output_suffix=".asc",
                input_suffixes=(".bin", ".bit"),
                package="fpga-icestorm",
                url="https://github.com/YosysHQ/icestorm",
                purpose="unpack the packed bitstream into icebox's ASCII representation",
            ),
            ConverterStep(
                program="icebox_vlog",
                arguments=("-n", "chip", "{input}"),
                output_suffix=".v",
                capture_stdout=True,
                input_suffixes=(".asc",),
                package="fpga-icestorm",
                url="https://github.com/YosysHQ/icestorm",
                purpose="recover the bitstream's logic as Verilog (behavioural: LUT equations, "
                "carry expressions and 'always @(posedge ...)' blocks, one per configured cell)",
            ),
            ConverterStep(
                program="yosys",
                arguments=(
                    "-q",
                    "-p",
                    "read_verilog {input}; synth_ice40 -top chip; "
                    "write_verilog -noattr -noexpr {output}",
                ),
                output_suffix=".v",
                input_suffixes=(".v",),
                package="yosys",
                url="https://github.com/YosysHQ/yosys",
                purpose="map icebox_vlog's behavioural Verilog onto the SB_* primitives HAL's "
                "netlist parser reads",
                version_argument="-V",
            ),
        ),
        gate_library="ice40ultra.hgl",
        status=STATUS_VERIFIED,
        notes=(
            "IceStorm's icebox_vlog writes *behavioural* Verilog -- LUT equations and always "
            "blocks -- not a cell netlist, and HAL's Verilog parser is a netlist parser, so the "
            "chain ends in 'yosys -p synth_ice40', which maps that logic back onto the "
            "SB_LUT4/SB_DFF*/SB_CARRY primitives ice40ultra.hgl defines. The result is the "
            "bitstream's logic, but re-mapped: cell count and instance names come from yosys, and "
            "names are positional (n17, io_7_0_1), never the designer's. Keep the intermediate "
            "files (--keep-intermediates) to see icebox_vlog's own output."
        ),
        documentation="https://clifford.at/icestorm",
    )
)

register(
    Family(
        key="ecp5",
        name="Lattice ECP5",
        vendor="Lattice",
        toolchain="Project Trellis",
        extensions=(".bit", ".config"),
        magic=(
            Magic(b"\xff\xff\xbd\xb3", "the ECP5 preamble FF FF BD B3"),
            Magic(b".device LFE5", "a Trellis ASCII config header (.device LFE5)", window=512),
        ),
        steps=(
            ConverterStep(
                program="ecpunpack",
                arguments=("{input}", "{output}"),
                output_suffix=".config",
                input_suffixes=(".bit",),
                package=None,  # not packaged for Debian/Ubuntu; build Trellis from source
                url="https://github.com/YosysHQ/prjtrellis",
                purpose="unpack the ECP5 bitstream into Trellis' ASCII config",
            ),
            ConverterStep(
                program="ecp_vlog",
                arguments=("{input}",),
                output_suffix=".v",
                capture_stdout=True,
                input_suffixes=(".config",),
                package=None,  # not packaged for Debian/Ubuntu; build Trellis from source
                url="https://github.com/YosysHQ/prjtrellis",
                purpose="recover the bitstream's logic as Verilog from the Trellis config",
            ),
            ConverterStep(
                program="yosys",
                arguments=(
                    "-q",
                    "-p",
                    "read_verilog {input}; synth_ecp5 -top top; "
                    "write_verilog -noattr -noexpr {output}",
                ),
                output_suffix=".v",
                input_suffixes=(".v",),
                package="yosys",
                url="https://github.com/YosysHQ/yosys",
                purpose="map ecp_vlog's Verilog onto ECP5 primitives HAL's netlist parser reads",
                version_argument="-V",
            ),
        ),
        gate_library=None,
        status=STATUS_UNVERIFIED,
        notes=(
            "Implemented from Trellis' documentation and NOT exercised in this fork: prjtrellis is "
            "not packaged for Debian/Ubuntu and is not installed in the CI containers, and this "
            "fork ships no ECP5 gate library, so the netlist has to be read with a user-supplied "
            "--gate-library. The chain mirrors the verified iCE40 one (unpack, recover Verilog, "
            "re-map it with yosys); correct the command lines here if a real run disagrees."
        ),
        documentation="https://prjtrellis.readthedocs.io",
    )
)

register(
    Family(
        key="nexus",
        name="Lattice Nexus (CertusPro/CrossLink)",
        vendor="Lattice",
        toolchain="Project Oxide",
        extensions=(".bit",),
        magic=(),
        steps=(),
        gate_library=None,
        status=STATUS_NO_CONVERTER,
        notes=(
            "Project Oxide documents the format and prjoxide can unpack a bitstream to FASM, but "
            "no open tool writes a Verilog netlist from it yet. Pass --family nexus explicitly: "
            "Nexus and ECP5 bitstreams share an extension and a preamble."
        ),
        documentation="https://github.com/gatecat/prjoxide",
    )
)

register(
    Family(
        key="gowin",
        name="Gowin LittleBee",
        vendor="Gowin",
        toolchain="Project Apicula",
        extensions=(".fs",),
        magic=(Magic(b"//Gowin", "an Apicula/Gowin ASCII bitstream header", window=256),),
        steps=(),
        gate_library=None,
        status=STATUS_NO_CONVERTER,
        notes=(
            "Apicula's gowin_unpack produces a chip database view, not a netlist. Register a "
            "converter here (registry.register(...)) when one exists."
        ),
        documentation="https://github.com/YosysHQ/apicula",
    )
)

register(
    Family(
        key="xilinx7",
        name="Xilinx 7-series",
        vendor="Xilinx/AMD",
        toolchain="Project X-Ray",
        extensions=(".bit", ".bin"),
        magic=(Magic(b"\xaa\x99\x55\x66", "the Xilinx sync word AA 99 55 66"),),
        steps=(),
        gate_library="XILINX_UNISIM.hgl",
        status=STATUS_NO_CONVERTER,
        notes=(
            "Project X-Ray documents the format and bitread/fasm2frames go the other way; there is "
            "no open bitstream-to-netlist writer. HAL reads X-Ray-derived netlists fine once one "
            "exists -- XILINX_UNISIM.hgl is the library to read them with."
        ),
        documentation="https://github.com/f4pga/prjxray",
    )
)
