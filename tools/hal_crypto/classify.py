"""Aggregate the passes into one family verdict and one classical/PQC verdict.

Each pass answers a narrow structural question.  This module puts the answers
side by side and applies one fixed, written-down rule set to them, so the
verdict is reproducible and arguable rather than a judgement call:

======================  =====================================================
family                  fires when
======================  =====================================================
``lattice-ntt``         a verified add/subtract butterfly over the same
                        operand registers **and** a carry chain with a
                        constant operand to read a modulus out of
``sponge``              an extracted S-box equals a sponge-family entry
                        (Ascon, Keccak chi) at any match tier
``spn``                 at least :data:`MIN_SBOX_INSTANCES` extracted S-boxes,
                        or one S-box plus a non-identity permutation layer --
                        either a pure-wire one or one
                        :func:`hal_crypto.permutation.cone_support_maps` read
                        through a round-key XOR, as long as it maps one vector
                        onto a *different* register bank (a bank that reads
                        itself rotated is a key schedule, not diffusion)
``arx``                 :mod:`hal_crypto.arx` returns ``arx-candidate``
``lfsr-stream``         a feedback shift register of at least
                        :data:`hal_crypto.shiftreg.MIN_CHAIN_LENGTH` stages
``none-detected``       none of the above
======================  =====================================================

When more than one fires, the *reported* family is the first in that order and
the rest are listed under ``families_present``; nothing is discarded.

The classical/PQC verdict is deliberately narrow.  ``pqc-style`` means
**lattice-style NTT / ring arithmetic is present in the structure**.  It does
not name a scheme: reducing modulo 3329 is what Kyber does and also what
anything else using that ring does, and a lattice accelerator with an
unpublished modulus is still a lattice accelerator.  Equally, ``classical-style``
means the structures found are the ones classical symmetric primitives are
built from -- it is not a statement that the design is not post-quantum, since
a hash-based or code-based scheme contains neither NTTs nor S-boxes and would
come back ``none-detected``.  That caveat travels in the finding text.

``sponge`` is the one family that is **not** placed on the axis at all, and it
gets ``undetermined`` rather than ``classical-style``.  A Keccak permutation is
SHA-3 and it is equally the SHAKE inside ML-KEM, ML-DSA and SPHINCS+; the
structure is identical either way, so calling it classical would be reporting a
coin toss as a measurement.  What decides the axis is the arithmetic *around*
the sponge, which is exactly what the other five passes look for.

``none-detected`` is a first-class result.  A netlist with a counter in it is
supposed to produce it, and the finding says which passes ran and what each one
did not find, so the negative is checkable rather than a shrug.
"""

from . import arx, findings, ntt, permutation, sbox, shiftreg
from .netlist_model import NetlistModel

from hal_findings import model

__all__ = [
    "FAMILIES",
    "MIN_SBOX_INSTANCES",
    "PRODUCER",
    "run_passes",
    "verdict",
    "build_document",
]

PRODUCER = {"name": "hal_crypto.classify", "version": "1.0.0"}

#: Reported family, most specific first.
FAMILIES = ("lattice-ntt", "sponge", "spn", "arx", "lfsr-stream", "none-detected")

#: How many extracted S-boxes make a substitution *layer* on their own.
MIN_SBOX_INSTANCES = 2

_CONFIDENCE_VALUE = {"low": 0.4, "medium": 0.6, "high": 0.85}


def run_passes(netlist):
    """Run every pass over *netlist* and return the raw evidence."""
    from . import arith

    model_view = NetlistModel(netlist)
    adders = arith.adders(model_view)
    permutations = permutation.find_permutations(model_view)
    return {
        "model": model_view,
        "sbox": sbox.identify(model_view),
        "shift": shiftreg.find_shift_structures(model_view),
        "permutations": permutations,
        # Kept in their own list, never merged into the wiring tier's: the ARX
        # pass reads "permutations" as rotations it may count, and a map that
        # needed a cone read is not evidence of the same strength.
        "cone_permutations": permutation.cone_support_maps(
            model_view, wiring=permutations
        ),
        "arx": arx.identify(model_view, adders=adders, permutations=permutations),
        "ntt": ntt.identify(model_view, adders=adders),
        "adders": adders,
    }


def _sbox_tier(entry):
    tiers = [match["tier"] for match in entry["matches"]]
    for tier in ("exact", "xor_constant", "bit_permutation"):
        if tier in tiers:
            return tier
    return None


def verdict(evidence):
    """Family and classical/PQC verdicts with the evidence that produced them."""
    sboxes = evidence["sbox"]["sboxes"]
    # A feedback register that absorbs an external bit every step is a CRC or a
    # scrambler, not an autonomous keystream generator: its polynomial is still
    # reported, but it does not make the design a stream cipher.
    feedback = [
        entry
        for entry in evidence["shift"]
        if entry["kind"] in ("lfsr", "nlfsr") and entry.get("autonomous", True)
    ]
    absorbing = [
        entry
        for entry in evidence["shift"]
        if entry["kind"] in ("lfsr", "nlfsr") and not entry.get("autonomous", True)
    ]
    plain_chains = [
        entry for entry in evidence["shift"] if entry["kind"] == "shift_register"
    ]
    layers = [
        entry
        for entry in evidence["permutations"]
        if entry["kind"] in ("rotation", "reversal", "general")
    ]
    # Layers recovered through one cell per link count towards the family --
    # a pLayer behind a round-key XOR is still a pLayer -- but they are said
    # out loud as what they are, in their own evidence line and their own
    # finding, because the wiring tier proves more.  A bank that reads *itself*
    # rotated is excluded from the family rule: that is a key schedule or a
    # shift structure, not the diffusion layer between two substitution layers,
    # and "one S-box plus a rotating register" is not an SPN.
    cone_layers = list(evidence.get("cone_permutations", ()))
    cone_diffusion = [
        entry
        for entry in cone_layers
        if entry["destination"] != "register bank {}".format(entry["source"])
    ]
    sponge_boxes = [
        entry for entry in sboxes if "sponge" in entry["families"]
    ]

    present = []
    evidence_lines = []
    confidence = "low"

    if evidence["ntt"]["verdict"] in (
        "lattice-style-modular-transform",
        "modular-arithmetic-candidate",
    ):
        present.append("lattice-ntt")
        named = evidence["ntt"]["named_moduli"]
        evidence_lines.append(
            "{} verified add/subtract butterfly pair(s) over the same operand "
            "registers, stage depth {}".format(
                evidence["ntt"]["butterfly_count"], evidence["ntt"]["stage_depth"]
            )
        )
        recovered = evidence["ntt"].get("recovered_moduli") or []
        if named:
            evidence_lines.append(
                "a carry chain subtracts a constant equal to the published modulus "
                "{}".format(", ".join(str(value) for value in named))
            )
            confidence = "high"
        elif recovered:
            evidence_lines.append(
                "the butterfly's conditional correction subtracts {}, derived from "
                "the reduction logic and checked on every sum the adder can "
                "produce -- a modulus, but not one in the built-in library of "
                "published parameters".format(
                    ", ".join(str(value) for value in recovered)
                )
            )
            confidence = _max_confidence(confidence, "medium")
        else:
            evidence_lines.append(
                "a carry chain with a constant operand is present but the constant "
                "matches no modulus in the built-in library"
            )
            confidence = _max_confidence(confidence, "medium")

    if sponge_boxes:
        present.append("sponge")
        for entry in sponge_boxes:
            evidence_lines.append(
                "a {}-bit substitution over {} equals {} ({} match)".format(
                    entry["bits"],
                    ", ".join(entry["sources"]),
                    ", ".join(
                        match["name"]
                        for match in entry["matches"]
                        if match["family"] == "sponge"
                    ),
                    _sbox_tier(entry),
                )
            )
        confidence = _max_confidence(confidence, "high")

    if len(sboxes) >= MIN_SBOX_INSTANCES or (sboxes and (layers or cone_diffusion)):
        present.append("spn")
        matched = [entry for entry in sboxes if entry["matches"]]
        evidence_lines.append(
            "{} bijective non-affine substitution(s) extracted from LUT cones, "
            "{} of them matching the built-in library".format(len(sboxes), len(matched))
        )
        if layers:
            evidence_lines.append(
                "{} non-identity bit permutation layer(s) between register "
                "banks".format(len(layers))
            )
        for entry in cone_layers:
            evidence_lines.append(_cone_layer_sentence(entry))
        confidence = _max_confidence(confidence, "high" if matched else "medium")

    if evidence["arx"]["verdict"] == "arx-candidate":
        present.append("arx")
        evidence_lines.append(
            "{} verified adder(s), {} fixed rotation(s) and {} XOR cell(s) "
            "({} standalone), wired into a round".format(
                len(evidence["arx"]["adders"]),
                len(evidence["arx"]["rotations"]),
                evidence["arx"]["xor_cells"],
                evidence["arx"].get(
                    "standalone_xor_cells", evidence["arx"]["xor_cells"]
                ),
            )
        )
        families = evidence["arx"].get("rotation_families") or []
        if families:
            evidence_lines.append(
                "the rotation amounts include the published set of {}".format(
                    ", ".join(entry["name"] for entry in families)
                )
            )
        confidence = _max_confidence(confidence, "high" if families else "medium")

    if feedback:
        present.append("lfsr-stream")
        for entry in feedback:
            if entry["kind"] == "lfsr" and entry.get("polynomial"):
                evidence_lines.append(
                    "a {}-stage {} LFSR with feedback polynomial {}{}{}".format(
                        entry["length"],
                        entry["form"],
                        entry["polynomial"],
                        ", maximal length" if entry.get("maximal_length") else "",
                        _mode_suffix(entry),
                    )
                )
            elif entry["kind"] == "lfsr":
                evidence_lines.append(
                    "a {}-stage linear feedback register coupled to chain(s) {}, "
                    "feedback {}{}".format(
                        entry["length"],
                        ", ".join(str(index) for index in entry["coupled_chains"]),
                        entry["feedback_anf"],
                        _mode_suffix(entry),
                    )
                )
            else:
                evidence_lines.append(
                    "a {}-stage NLFSR with feedback ANF {}{}{}".format(
                        entry["length"],
                        entry["feedback_anf"],
                        " (coupled to chain(s) {})".format(
                            ", ".join(str(index) for index in entry["coupled_chains"])
                        )
                        if entry.get("coupled")
                        else "",
                        _mode_suffix(entry),
                    )
                )
        confidence = _max_confidence(confidence, "high")

    family = next((name for name in FAMILIES if name in present), "none-detected")

    if family == "none-detected":
        confidence = "high" if _passes_were_conclusive(evidence) else "medium"
        evidence_lines = _negative_evidence(evidence, plain_chains, absorbing, cone_layers)
        style = "undetermined"
        style_text = (
            "No structure this package recognises as cryptographic was found, so "
            "there is nothing to place on the classical/PQC axis. That is a "
            "statement about the six passes below, not a proof that the design "
            "computes no cryptography."
        )
    elif "lattice-ntt" in present:
        style = "pqc-style"
        style_text = (
            "Lattice-style NTT / ring arithmetic is present: butterflies over a "
            "small modulus are the structure post-quantum lattice schemes are "
            "built from. This does NOT identify a scheme -- the same ring "
            "arithmetic appears wherever that ring is used."
        )
        if not evidence["ntt"]["named_moduli"]:
            style_text += (
                " And the modulus found is in no published-parameter library, so "
                "this is the *shape* of a lattice scheme's arithmetic and not any "
                "deployed one's parameters: read it as 'the kernel a lattice "
                "scheme is built out of', not as 'a post-quantum implementation'."
            )
    elif "sponge" in present:
        style = "undetermined"
        style_text = (
            "A sponge permutation was found and no lattice/ring arithmetic was. "
            "That places the design on neither side of the classical/PQC axis, "
            "because the same permutation sits on both: SHA-3 and SHAKE are "
            "classical hashing, and SHAKE is also the extendable-output function "
            "inside ML-KEM and ML-DSA and the whole of SPHINCS+. 'A Keccak core "
            "is present' is therefore evidence about what the design computes "
            "and no evidence at all about which family of scheme uses it -- the "
            "surrounding arithmetic decides that, and none was found here. "
            "Structures found: {}.".format(", ".join(present))
        )
    else:
        style = "classical-style"
        style_text = (
            "The structures found ({}) are the ones classical symmetric "
            "primitives are built from, and no lattice/ring arithmetic was "
            "found. This does NOT rule out post-quantum cryptography: a "
            "hash-based or code-based scheme contains neither NTTs nor S-boxes "
            "and would come back none-detected here.".format(", ".join(present))
        )

    return {
        "family": family,
        "families_present": present,
        "style": style,
        "style_rationale": style_text,
        "evidence": evidence_lines,
        "confidence": confidence,
        "confidence_value": _CONFIDENCE_VALUE[confidence],
    }


def _mode_suffix(entry):
    """", while <net> = <value>" when the structure needed a net held."""
    mode = entry.get("mode")
    if not mode:
        return ""
    return ", while {} = {}".format(mode["net"], mode["value"])


def _mode_sentence(entry):
    """The sentence that turns a structural claim into a claim about a mode."""
    mode = entry.get("mode")
    if not mode:
        return ""
    return (
        " The shift links are only visible with {} held at {}: every stage's next "
        "state is a multiplexer, which is what a parallel load looks like after "
        "synthesis. The claim is therefore about the netlist in that operating "
        "mode.".format(mode["net"], mode["value"])
    )


def _cone_layer_sentence(entry):
    """One evidence line for a map read off the next-state cone support."""
    names = ", ".join(match["name"] for match in entry["matches"])
    if entry["kind"] == "rotation":
        what = "reads {} rotated left by {}".format(
            entry["source"], entry["rotate_left_by"]
        )
    else:
        what = "is a {}-bit {} permutation of {}".format(
            entry["width"], entry["kind"], entry["source"]
        )
    return (
        "the next-state cone of {} {} ({} of {} bits link back, each through one "
        "cell with other operands){}".format(
            entry["destination"],
            what,
            entry["bits_observed"],
            entry["width"],
            ", equal to the published {}".format(names) if names else "",
        )
    )


def _max_confidence(current, candidate):
    order = ("low", "medium", "high")
    return candidate if order.index(candidate) > order.index(current) else current


def _passes_were_conclusive(evidence):
    """True when every pass ran without hitting a coverage limit."""
    return not evidence["sbox"]["rejected"]


def _negative_evidence(evidence, plain_chains, absorbing=(), cone_layers=()):
    lines = [
        "S-box extraction: {} bijective non-affine substitution(s) of 3..8 bits "
        "found in the LUT cones".format(len(evidence["sbox"]["sboxes"])),
        "shift registers: {} chain(s) of at least {} stages, {} of them closed by "
        "a feedback function".format(
            len(evidence["shift"]),
            shiftreg.MIN_CHAIN_LENGTH,
            len([entry for entry in evidence["shift"] if entry["kind"] != "shift_register"]),
        ),
        "ARX: " + "; ".join(evidence["arx"].get("missing", ["no ingredient missing"])),
        "modular arithmetic: "
        + "; ".join(evidence["ntt"].get("missing", ["no ingredient missing"])),
        "permutation layers: {} non-identity bit map(s) between equally wide "
        "vectors".format(
            len(
                [
                    entry
                    for entry in evidence["permutations"]
                    if entry["kind"] != "identity"
                ]
            )
        ),
    ]
    for entry in cone_layers:
        lines.append(_cone_layer_sentence(entry))
    if plain_chains:
        lines.append(
            "{} shift chain(s) with no feedback at all, which is a shift register "
            "and not a generator".format(len(plain_chains))
        )
    for entry in absorbing:
        lines.append(
            "a {}-stage feedback register that absorbs {} every step, feedback "
            "polynomial {} (reciprocal {}): a CRC/scrambler shape, not an "
            "autonomous keystream generator".format(
                entry["length"],
                ", ".join(entry.get("data_inputs", [])),
                entry.get("polynomial", "nonlinear"),
                entry.get("polynomial_reciprocal", "-"),
            )
        )
    return lines


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def _scope(artifact_id, description=None, gate_types=None):
    return model.scope(
        [artifact_id],
        description=description,
        gate_types=gate_types or ["tennm_ff", "tennm_lcell_comb"],
    )


def sbox_findings(artifact_id, result):
    items = []
    for number, entry in enumerate(result["sboxes"]):
        suffix = "" if len(result["sboxes"]) == 1 else "/{}".format(number)
        data = {
            "bits": entry["bits"],
            "sources": entry["sources"],
            "outputs": entry["outputs"],
            "table": entry["table"],
            "algebraic_degree": entry["algebraic_degree"],
            "differential_uniformity": entry["differential_uniformity"],
            "matches": entry["matches"],
            "bit_permutation_search": entry["search_note"],
        }
        if entry["matches"]:
            tier = _sbox_tier(entry)
            names = ", ".join(match["name"] for match in entry["matches"])
            items.append(
                model.finding(
                    "hal_crypto/sbox/library-match" + suffix,
                    "A {}-bit substitution equal to {} ({} match)".format(
                        entry["bits"], names, tier
                    ),
                    model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
                    findings.EXACT_EVALUATION,
                    _scope(
                        artifact_id,
                        description="LUT cones over {}".format(", ".join(entry["sources"])),
                    ),
                    summary=(
                        "The {} output cones over {} were enumerated over all {} input "
                        "assignments; the resulting bijection equals {} under the "
                        "'{}' equivalence. Table equality is a fact about the extracted "
                        "function -- it does not by itself make the design that "
                        "cipher.".format(
                            entry["bits"],
                            ", ".join(entry["sources"]),
                            1 << entry["bits"],
                            names,
                            tier,
                        )
                    ),
                    bounds_dict=model.unbounded(
                        description=(
                            "the cones are combinational, so enumerating their input "
                            "space covers every evaluation of them"
                        )
                    ),
                    assumptions=[
                        findings.PRIMITIVE_SEMANTICS_ASSUMPTION,
                        findings.READER_ASSUMPTION,
                        model.assumption(
                            "bit-order",
                            "Input and output bits were ordered by net name, which is "
                            "the pass's own convention; a match at the "
                            "'bit_permutation' tier is exactly the statement that a "
                            "different order would make the tables equal.",
                            kind="structural",
                        ),
                    ],
                    data=data,
                    tags=["crypto", "sbox"],
                )
            )
        else:
            items.append(
                model.finding(
                    "hal_crypto/sbox/unmatched" + suffix,
                    "A {}-bit bijective non-affine substitution with no library "
                    "match".format(entry["bits"]),
                    model.STATUS_HEURISTIC,
                    findings.EXACT_EVALUATION,
                    _scope(artifact_id),
                    summary=(
                        "{} cones over {} form a bijection of degree {} that matches no "
                        "entry of the built-in library at any tier. That is what an "
                        "unpublished S-box looks like and also what some non-cipher "
                        "bijections look like.".format(
                            entry["bits"],
                            ", ".join(entry["sources"]),
                            entry["algebraic_degree"],
                        )
                    ),
                    confidence=0.5,
                    data=data,
                    tags=["crypto", "sbox"],
                )
            )
    if not items:
        items.append(
            model.finding(
                "hal_crypto/sbox/none",
                "No substitution box found",
                model.STATUS_UNKNOWN,
                findings.EXACT_EVALUATION,
                _scope(artifact_id),
                summary=(
                    "No group of 3..8 combinational cones over the same 3..8 sources "
                    "forms a bijection that is not affine. {} group(s) were formed and "
                    "rejected.".format(len(result["rejected"]))
                ),
                data={"rejected": result["rejected"][:10]},
                tags=["crypto", "sbox"],
            )
        )
    return items


def shift_findings(artifact_id, structures):
    items = []
    if not structures:
        return [
            model.finding(
                "hal_crypto/shift-register/none",
                "No shift chain of at least {} stages".format(shiftreg.MIN_CHAIN_LENGTH),
                model.STATUS_UNKNOWN,
                findings.STRUCTURAL,
                _scope(artifact_id, gate_types=["tennm_ff"]),
                summary=(
                    "No run of registers wired q-to-d through buffers or inverters "
                    "reaches {} stages, so there is no shift register to classify."
                    .format(shiftreg.MIN_CHAIN_LENGTH)
                ),
                tags=["crypto", "lfsr"],
            )
        ]
    for number, entry in enumerate(structures):
        suffix = "" if len(structures) == 1 else "/{}".format(number)
        data = {key: value for key, value in entry.items() if key != "model"}
        if entry["kind"] == "lfsr":
            items.append(
                model.finding(
                    "hal_crypto/lfsr/polynomial" + suffix,
                    "A {}-stage {} {}LFSR with {}".format(
                        entry["length"],
                        entry["form"],
                        "" if entry.get("autonomous", True) else "data-absorbing ",
                        "feedback polynomial {}".format(entry["polynomial"])
                        if entry.get("polynomial")
                        else "feedback {} over coupled chain(s) {}".format(
                            entry["feedback_anf"],
                            ", ".join(
                                str(index) for index in entry["coupled_chains"]
                            ),
                        ),
                    ),
                    model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
                    findings.EXACT_EVALUATION,
                    _scope(
                        artifact_id,
                        description="registers {} .. {}".format(entry["head"], entry["tail"]),
                    ),
                    summary=(
                        "{} registers shift into one another and the feedback is the "
                        "XOR of stages {}. The feedback cone was enumerated over its "
                        "whole input space, so the tap set is read off the function, "
                        "not guessed from the wiring. Convention: {}{}{}{}".format(
                            entry["length"],
                            ", ".join(str(tap) for tap in entry["taps"]),
                            entry["polynomial_convention"],
                            ". "
                            if not entry.get("polynomial")
                            else ", which makes the same recurrence read {} from the "
                            "other end.".format(entry["polynomial_reciprocal"]),
                            " Iterating the recurrence gives period {}{}.".format(
                                entry["period"],
                                " = 2^{} - 1, maximal".format(entry["length"])
                                if entry.get("maximal_length")
                                else "",
                            )
                            if entry.get("period")
                            else "",
                            ""
                            if entry.get("autonomous", True)
                            else " The register also absorbs {} every step, so it is a "
                            "CRC/scrambler shape rather than an autonomous "
                            "generator.".format(", ".join(entry.get("data_inputs", []))),
                        )
                        + _mode_sentence(entry)
                    ),
                    bounds_dict=model.unbounded(
                        description=(
                            "a statement about the next-state function, which was "
                            "evaluated exhaustively"
                        )
                    ),
                    assumptions=[
                        findings.PRIMITIVE_SEMANTICS_ASSUMPTION,
                        findings.READER_ASSUMPTION,
                        model.assumption(
                            "state-encoding",
                            "Stage i's logical value is taken as q_i XOR c_i, with c the "
                            "running parity of the inverters along the chain; the "
                            "reported encoding is '{}'. The tap set does not depend on "
                            "that choice, the constant term does.".format(
                                entry["state_encoding"]
                            ),
                            kind="structural",
                        ),
                    ],
                    data=data,
                    tags=["crypto", "lfsr"],
                )
            )
        elif entry["kind"] == "nlfsr":
            items.append(
                model.finding(
                    "hal_crypto/nlfsr/feedback" + suffix,
                    "A {}-stage NLFSR: the feedback has degree {}{}".format(
                        entry["length"],
                        entry["feedback_degree"],
                        " and is coupled to chain(s) {}".format(
                            ", ".join(str(index) for index in entry["coupled_chains"])
                        )
                        if entry.get("coupled")
                        else "",
                    ),
                    model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
                    findings.EXACT_EVALUATION,
                    _scope(artifact_id),
                    summary=(
                        "The chain is closed by a feedback function that is not linear, "
                        "so it has no feedback polynomial. Its algebraic normal form is "
                        "{}.{}".format(
                            entry["feedback_anf"],
                            " {} of its inputs are stages of chain(s) {} rather than of "
                            "this one, so the register is closed through a sibling: a "
                            "Trivium/Grain-style coupled generator, whose state is the "
                            "union of the coupled chains.".format(
                                len(entry["coupled_taps"]),
                                ", ".join(
                                    str(index) for index in entry["coupled_chains"]
                                ),
                            )
                            if entry.get("coupled")
                            else "",
                        )
                        + _mode_sentence(entry)
                    ),
                    bounds_dict=model.unbounded(
                        description="the feedback cone was enumerated exhaustively"
                    ),
                    assumptions=[
                        findings.PRIMITIVE_SEMANTICS_ASSUMPTION,
                        findings.READER_ASSUMPTION,
                    ],
                    data=data,
                    tags=["crypto", "nlfsr"],
                )
            )
        else:
            items.append(
                model.finding(
                    "hal_crypto/shift-register/open" + suffix,
                    "A {}-stage shift register with no feedback".format(entry["length"]),
                    model.STATUS_HEURISTIC,
                    findings.STRUCTURAL,
                    _scope(artifact_id, gate_types=["tennm_ff"]),
                    summary=(
                        "{} registers shift into one another, but the chain is not "
                        "closed: {}".format(entry["length"], entry["reason"])
                    ),
                    confidence=0.6,
                    data=data,
                    tags=["crypto", "lfsr"],
                )
            )
    return items


def arx_findings(artifact_id, result):
    if result["verdict"] == "arx-candidate":
        return [
            model.finding(
                "hal_crypto/arx/round",
                "Add-Rotate-XOR round structure",
                model.STATUS_HEURISTIC,
                findings.sampled_method(
                    max((entry["checked"] for entry in result["adders"]), default=0)
                ),
                _scope(artifact_id),
                summary=(
                    "{} verified adder(s), {} fixed rotation(s) ({} of them read off a "
                    "named vector, the rest off the order a cell layer reads a register "
                    "bank) and {} XOR cell(s) ({} standalone, {} recovered by holding one "
                    "packed multiplexer input) are present and wired to one another. This "
                    "is the shape of an ARX round; it is not a proof that the design is a "
                    "cipher, and it names no algorithm.".format(
                        len(result["adders"]),
                        len(result["rotations"]),
                        sum(
                            1
                            for entry in result["rotations"]
                            if entry.get("read_from") == "named vector"
                        ),
                        result["xor_cells"],
                        result.get("standalone_xor_cells", result["xor_cells"]),
                        result.get("conditional_xor_cells", 0),
                    )
                ),
                confidence=0.65,
                data=result,
                tags=["crypto", "arx"],
            )
        ]
    return [
        model.finding(
            "hal_crypto/arx/not-arx",
            "No ARX round structure",
            model.STATUS_UNKNOWN,
            findings.STRUCTURAL,
            _scope(artifact_id),
            summary="ARX needs adders, rotations and an XOR layer together; "
            + "; ".join(result.get("missing", [])),
            data=result,
            tags=["crypto", "arx"],
        )
    ]


def _cone_permutation_finding(artifact_id, entry, suffix):
    names = ", ".join(match["name"] for match in entry["matches"])
    side = entry["shared_side_inputs"]
    return model.finding(
        "hal_crypto/permutation/cone-support" + suffix,
        (
            "{} reads {} rotated left by {}, recovered from the next-state cone "
            "support".format(
                entry["destination"], entry["source"], entry["rotate_left_by"]
            )
            if entry["kind"] == "rotation"
            else "A {}-bit {} bit map from {} into {}, recovered from the "
            "next-state cone support{}".format(
                entry["width"],
                entry["kind"],
                entry["source"],
                entry["destination"],
                " matching {}".format(names) if names else "",
            )
        ),
        model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
        findings.EXACT_EVALUATION,
        _scope(artifact_id, description=entry["destination"]),
        summary=(
            "{} of the {} bits of {} have a next-state cone that reads exactly one "
            "bit of {}, and the index map is {}. The cones were enumerated over "
            "their whole input space, so 'reads exactly one bit' is read off the "
            "function and not off the wiring -- which is the point: every link "
            "here passes through a cell with other operands ({}), so the wiring "
            "tier sees nothing at all. This is the weaker claim of the two: it "
            "says which bit each register reads, not that the map is the wiring."
            "{}{}".format(
                entry["bits_observed"],
                entry["width"],
                entry["destination"],
                entry["source"],
                "a rotation left by {}".format(entry["rotate_left_by"])
                if entry["kind"] == "rotation"
                else "a {} permutation".format(entry["kind"]),
                "at most {} other net(s) per link{}".format(
                    entry["max_side_inputs"],
                    ", {} of them shared by every link".format(len(side))
                    if side
                    else "",
                ),
                " It equals the published {}.".format(names) if names else "",
                " Every link is an XOR of the source bit with those operands."
                if entry["link_form"] == "xor"
                else " The links are gated, not pure XORs: the source bit reaches "
                "the register in one operating mode of the side inputs, which is "
                "what a parallel load or an enable looks like after synthesis.",
            )
        ),
        bounds_dict=model.unbounded(
            description=(
                "a statement about the next-state functions, each evaluated "
                "exhaustively over the cut described below"
            )
        ),
        assumptions=[
            findings.PRIMITIVE_SEMANTICS_ASSUMPTION,
            findings.READER_ASSUMPTION,
            model.assumption(
                "one-side-input-per-link",
                "Each link is 'this register's next state depends on exactly one "
                "bit of {}', modulo whatever else the cone reads -- a round-key "
                "operand, a load path, a mode select. The recovered map is "
                "therefore the map the design applies in the operating mode where "
                "the source reaches the register, not an unconditional wire "
                "permutation. A pure-wire map, when one exists, is reported "
                "separately as hal_crypto/permutation/layer, which is the stronger "
                "claim.".format(entry["source"]),
                kind="structural",
            ),
            model.assumption(
                "vector-cut",
                "The cone walk stops at every vector the export declares that is "
                "at least {} bits wide, so '{}' is the layer named in the netlist. "
                "A synthesiser that dissolved that vector would leave this map "
                "unrecoverable -- which is a fact about the export, not about the "
                "design.".format(permutation.MIN_PERMUTATION_WIDTH, entry["source"]),
                kind="structural",
            ),
        ]
        + (
            []
            if entry["complete"]
            else [
                model.assumption(
                    "partial-coverage",
                    "{} of the {} bits link back to {}; the other {} read more than "
                    "one bit of it or none, so they are not claimed. Only a single "
                    "consistent rotation amount is reported from a partial map -- a "
                    "general permutation cannot be extrapolated from one.".format(
                        entry["bits_observed"],
                        entry["width"],
                        entry["source"],
                        entry["width"] - entry["bits_observed"],
                    ),
                    kind="structural",
                )
            ]
        ),
        data=entry,
        tags=["crypto", "permutation"],
    )


def permutation_findings(artifact_id, permutations, cone_maps=()):
    """The wiring tier's findings, with the cone-support tier's in front of them.

    The two are separate findings on purpose: one says the map *is* the wiring,
    the other says which bit each register reads. Never one finding averaging
    the two.
    """
    cone_maps = list(cone_maps)
    cone_items = [
        _cone_permutation_finding(
            artifact_id, entry, "" if len(cone_maps) == 1 else "/{}".format(number)
        )
        for number, entry in enumerate(cone_maps)
    ]
    interesting = [entry for entry in permutations if entry["kind"] != "identity"]
    if not interesting:
        data = {"maps_examined": len(permutations)}
        if cone_maps:
            data["cone_support_maps"] = len(cone_maps)
        return cone_items + [
            model.finding(
                "hal_crypto/permutation/none",
                "No non-identity bit permutation layer",
                model.STATUS_UNKNOWN,
                findings.STRUCTURAL,
                _scope(artifact_id),
                summary=(
                    "Every pure-wire map between two equally wide vectors is the "
                    "identity, so there is no permutation layer to compare against the "
                    "published pLayers."
                    + (
                        ""
                        if not cone_maps
                        else " {} map(s) were recovered from the next-state cone "
                        "support instead and are reported separately: the silence "
                        "here is about wires, not about the design.".format(
                            len(cone_maps)
                        )
                    )
                ),
                data=data,
                tags=["crypto", "permutation"],
            )
        ]
    items = []
    for number, entry in enumerate(interesting):
        suffix = "" if len(interesting) == 1 else "/{}".format(number)
        names = ", ".join(match["name"] for match in entry["matches"])
        items.append(
            model.finding(
                "hal_crypto/permutation/layer" + suffix,
                "A {}-bit {} between {} and {}{}".format(
                    entry["width"],
                    entry["kind"],
                    entry["source"],
                    entry["destination"],
                    " matching {}".format(names) if names else "",
                ),
                model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
                findings.STRUCTURAL,
                _scope(artifact_id),
                summary=(
                    "Each of the {} destination bits peels back through buffers and "
                    "inverters alone to a distinct bit of {}. The map is exactly the "
                    "wiring; nothing is inferred.{}".format(
                        entry["width"],
                        entry["source"],
                        " It equals the published {}.".format(names) if names else "",
                    )
                ),
                bounds_dict=model.unbounded(
                    description="a statement about the netlist wiring, not an execution"
                ),
                assumptions=[findings.READER_ASSUMPTION],
                data=entry,
                tags=["crypto", "permutation"],
            )
        )
    return cone_items + items


def ntt_findings(artifact_id, result):
    if result["verdict"] == "no-modular-transform":
        return [
            model.finding(
                "hal_crypto/ntt/none",
                "No modular-arithmetic transform",
                model.STATUS_UNKNOWN,
                findings.STRUCTURAL,
                _scope(artifact_id),
                summary="A butterfly needs an add and a subtract over the same "
                "operands, and a modulus needs a constant operand; "
                + "; ".join(result.get("missing", [])),
                data=result,
                tags=["crypto", "ntt"],
            )
        ]
    moduli = result["named_moduli"]
    recovered = result.get("recovered_moduli") or []
    if moduli:
        qualifier = " with modulus {}".format(
            ", ".join(str(value) for value in moduli)
        )
    elif recovered:
        qualifier = " with modulus {}, in no published-parameter library".format(
            ", ".join(str(value) for value in recovered)
        )
    else:
        qualifier = " with an unrecognised constant modulus"
    return [
        model.finding(
            "hal_crypto/ntt/butterfly",
            "{} NTT-style butterfly pair(s){}".format(
                result["butterfly_count"], qualifier
            ),
            model.STATUS_HEURISTIC,
            findings.sampled_method(
                min(
                    (pair["vectors_checked"] for pair in result["butterflies"]),
                    default=0,
                )
            ),
            _scope(artifact_id),
            summary=(
                "The same operand registers feed one chain that was verified to "
                "compute a + b and one that was verified to compute a - b, at stage "
                "depth {}. {} This is lattice-style ring arithmetic; it does not "
                "identify a scheme.".format(
                    result["stage_depth"],
                    "A constant-operand chain subtracts {}.".format(
                        ", ".join(str(value) for value in moduli)
                    )
                    if moduli
                    else (
                        "No constant-operand chain carries it, but the conditional "
                        "correction on the butterfly subtracts {} -- derived from "
                        "the reduction logic and checked on every sum the adder "
                        "can produce. It is in no library entry.".format(
                            ", ".join(str(value) for value in recovered)
                        )
                        if recovered
                        else "The constant-operand chain's constant is in no "
                        "library entry."
                    ),
                )
            ),
            confidence=0.75 if moduli else 0.55,
            data=result,
            tags=["crypto", "ntt", "pqc"],
        )
    ]


def verdict_findings(artifact_id, decision):
    family = decision["family"]
    return [
        model.finding(
            "hal_crypto/identify/family",
            "Structural family: {}".format(family),
            model.STATUS_UNKNOWN if family == "none-detected" else model.STATUS_HEURISTIC,
            findings.STRUCTURAL,
            _scope(artifact_id, description="the whole netlist"),
            summary=(
                "The six passes agree on '{}'. Families with evidence: {}. Every line "
                "of the evidence list below is a structural observation; none of them "
                "names an algorithm.".format(
                    family,
                    ", ".join(decision["families_present"]) or "none",
                )
            ),
            confidence=None if family == "none-detected" else decision["confidence_value"],
            data={
                "family": family,
                "families_present": decision["families_present"],
                "evidence": decision["evidence"],
                "confidence_tier": decision["confidence"],
            },
            tags=["crypto", "identify"],
        ),
        model.finding(
            "hal_crypto/identify/classical-vs-pqc",
            "Classical / PQC style: {}".format(decision["style"]),
            model.STATUS_UNKNOWN
            if decision["style"] == "undetermined"
            else model.STATUS_HEURISTIC,
            findings.STRUCTURAL,
            _scope(artifact_id, description="the whole netlist"),
            summary=decision["style_rationale"],
            confidence=None
            if decision["style"] == "undetermined"
            else decision["confidence_value"],
            data={
                "style": decision["style"],
                "evidence": decision["evidence"],
                "confidence_tier": decision["confidence"],
            },
            tags=["crypto", "identify", "pqc"],
        ),
    ]


def build_document(netlist, artifact, generated_at=None):
    """Run every pass and emit one findings document for the netlist."""
    artifact_id = artifact["artifact_id"]
    evidence = run_passes(netlist)
    decision = verdict(evidence)
    items = []
    items.extend(verdict_findings(artifact_id, decision))
    items.extend(sbox_findings(artifact_id, evidence["sbox"]))
    items.extend(shift_findings(artifact_id, evidence["shift"]))
    items.extend(
        permutation_findings(
            artifact_id,
            evidence["permutations"],
            cone_maps=evidence.get("cone_permutations", ()),
        )
    )
    items.extend(arx_findings(artifact_id, evidence["arx"]))
    items.extend(ntt_findings(artifact_id, evidence["ntt"]))
    return findings.document(
        PRODUCER,
        artifact,
        items,
        "hal_crypto.classify.build_document",
        generated_at=generated_at,
        notes=[
            "Structure-bounded claims only: every finding is about what the "
            "netlist is wired to compute, never about what the design is for.",
            "none-detected is a result, not a failure: it means none of the six "
            "passes fired, and the per-pass findings say what each one looked for.",
        ],
    )
