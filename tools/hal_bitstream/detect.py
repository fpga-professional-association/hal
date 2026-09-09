"""Which family a file belongs to -- decided by its contents, not by its name.

Extensions are useless here: ``.bit`` is Xilinx, ECP5 *and* Nexus, ``.bin`` is iCE40 *and* Xilinx.
So detection reads the head of the file and looks for the family's magic, and only falls back to
the extension when that is unambiguous. When nothing matches, the error lists what was looked for
-- a wrong guess about a device family wastes far more time than a refusal.
"""

import os

from . import registry

__all__ = ["DetectionError", "Detection", "sniff", "detect_family"]

HEAD_BYTES = 8192


class DetectionError(RuntimeError):
    """The family could not be determined (or the file cannot be read)."""


class Detection(object):
    def __init__(self, family, evidence, method, candidates=()):
        self.family = family
        self.evidence = evidence
        self.method = method
        self.candidates = tuple(candidates)

    def to_json(self):
        return {
            "family": self.family.key,
            "name": self.family.name,
            "vendor": self.family.vendor,
            "toolchain": self.family.toolchain,
            "status": self.family.status,
            "method": self.method,
            "evidence": self.evidence,
            "gate_library": self.family.gate_library,
            "candidates": [family.key for family in self.candidates],
        }


def _read_head(path):
    try:
        with open(str(path), "rb") as handle:
            return handle.read(HEAD_BYTES)
    except OSError as error:
        raise DetectionError("could not read {}: {}".format(path, error))


def sniff(path):
    """Return a :class:`Detection` for ``path``, or raise :class:`DetectionError`."""
    path = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(path):
        raise DetectionError("bitstream file does not exist: {}".format(path))
    if os.path.getsize(path) == 0:
        raise DetectionError("bitstream file is empty: {}".format(path))

    head = _read_head(path)
    for family in registry.families():
        for magic in family.magic:
            if magic.matches(head):
                return Detection(family, magic.evidence(head), "magic")

    extension = os.path.splitext(path)[1].lower()
    candidates = registry.families_for_extension(extension)
    if len(candidates) == 1:
        return Detection(
            candidates[0],
            "no magic matched; {} is only used by this family".format(extension or "no extension"),
            "extension",
            candidates,
        )

    known = ", ".join(
        "{} ({})".format(family.key, "/".join(family.extensions) or "no extension")
        for family in registry.families()
    )
    if candidates:
        raise DetectionError(
            "{}: the extension {} is used by {} and no magic matched, so the family is "
            "ambiguous. Pass --family <{}> to say which one it is.\nKnown families: {}".format(
                os.path.basename(path),
                extension,
                " and ".join(family.name for family in candidates),
                "|".join(family.key for family in candidates),
                known,
            )
        )
    raise DetectionError(
        "{}: not a bitstream this fork recognises. None of the known magic bytes were found in "
        "the first {} bytes and the extension {!r} matches no family.\nKnown families: {}\n"
        "If the format is right but the magic is not (a header was stripped, a new device), pass "
        "--family <key> to skip detection; if it is a family hal_bitstream does not know yet, add "
        "it to tools/hal_bitstream/registry.py.".format(
            os.path.basename(path), HEAD_BYTES, extension, known
        )
    )


def detect_family(path, override=None):
    """Detection with an explicit ``--family`` override; the override never lies to the user.

    When both are available and they disagree, the override wins and the disagreement is reported
    in the evidence -- silently ignoring a mismatch is how a Xilinx bitstream ends up 'converted'
    by an iCE40 tool.
    """
    if override:
        family = registry.get(override)
        try:
            detected = sniff(path)
        except DetectionError:
            return Detection(family, "family forced with --family {}".format(override), "forced")
        if detected.family.key == family.key:
            return Detection(family, detected.evidence, "forced-confirmed")
        return Detection(
            family,
            "forced with --family {}, but the file looks like {} ({})".format(
                override, detected.family.key, detected.evidence
            ),
            "forced-mismatch",
        )
    return sniff(path)
