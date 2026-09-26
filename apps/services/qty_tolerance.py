"""Over-delivery allowance for material bought by weight (project owner,
2026-09-26).

Steam coal, HM plastic and HDPE arrive by the truckload and are weighed at
the weighbridge, so a receipt a little over the ordered quantity is how the
purchase works, not a discrepancy: "the quantity is 100 and if we receive
110 it would show qty matched". Every other material keeps the zero
tolerance FLAG_DIFF_PCT sets.

The allowance is one-sided. Receiving up to BULK_QTY_OVER_TOLERANCE_PCT
MORE than the PO quantity counts as matched; receiving less is still a
shortfall at zero tolerance, because a short blanket order is exactly what
the Short-Delivered flag exists to show.

The material is recognised from the PO line's description, and not by an
exact string (project owner: "don't keep it exact as there can be human
error in the loop"). The same material is typed several ways across the
three plants' PO sheets - "Steam Coal Imported (Non Cooking)", "Imported
Coal", "HM PLASTIC 1600MMx51MICRON", "HDPE LAMINATED FABRIC", "H.D.P.E" - so
matching runs on normalised tokens with a small edit-distance allowance on
the longer words. Three look-alikes on the live data are deliberately NOT
matched, and each has a test:

  - "RUBBOND HM-65" / "HMMM65" (Vapi) - HM there is a melamine resin, so a
    bare "HM" is never enough; it must be followed by plastic.
  - "SULPHUR POWDER, HDPE Bags 50kg" (Vapi) - HDPE is the packaging of
    something else, so HDPE followed by bag/bags/packing does not count.
  - "LD Plastic Bag", "CP PLASTICIZER" - plastic without the HM prefix.

Kept free of Django imports, like parsers/common.py, so a migration or a
plain script can import it.
"""

import re
from decimal import Decimal
from difflib import SequenceMatcher
from functools import lru_cache

# How far over the ordered quantity a weighed material may come in and still
# read as matched. Mirrored as BULK_QTY_TOLERANCE_PCT in frontend/js/flags.js.
BULK_QTY_OVER_TOLERANCE_PCT = Decimal("10")

# A word this similar to the one intended is taken as a typo of it. Only the
# longer words get it; short ones (coal, hm, hdpe) must be spelled exactly,
# since one letter off "coal" is "coat", "cool" or "goal".
_TYPO_RATIO = 0.8

# A word after HDPE that says HDPE is the container, not the material bought.
_PACKAGING_WORDS = frozenset({"bag", "bags", "packing", "packed", "pack", "liner", "liners"})


def _similar(word: str, target: str) -> bool:
    return word == target or SequenceMatcher(None, word, target).ratio() >= _TYPO_RATIO


def _tokens(description: str) -> list[str]:
    """Lower-case words. Dots are removed first so "H.D.P.E." reads as one
    word, and a digit run is split from letters ("hm65" -> "hm", "65")."""
    text = (description or "").lower().replace(".", "")
    text = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", text)
    return re.findall(r"[a-z0-9]+", text)


def _is_coal(tokens: list[str]) -> bool:
    """Any coal word spelled right, or "steam" followed by a near-"coal"
    ("Steam Col"). "steamcoal" written as one word counts too."""
    for i, tok in enumerate(tokens):
        if tok in ("coal", "coals") or _similar(tok, "steamcoal"):
            return True
        if i and _similar(tokens[i - 1], "steam") and SequenceMatcher(None, tok, "coal").ratio() >= 0.85:
            return True
    return False


def _is_hm_plastic(tokens: list[str]) -> bool:
    """HM, then plastic within the next word: "HM PLASTIC", "HM-Plastics",
    "H M Plastic", "HMPLASTIC", "HM Plastik". HM followed by anything else
    (HM-65 resin) is not this material."""
    for i, tok in enumerate(tokens):
        if tok.startswith("hm") and len(tok) > 2 and any(_similar(tok[2:], p) for p in ("plastic", "plastics")):
            return True
        prefix_end = None
        if tok == "hm":
            prefix_end = i
        elif tok == "h" and i + 1 < len(tokens) and tokens[i + 1] == "m":
            prefix_end = i + 1
        if prefix_end is not None and prefix_end + 1 < len(tokens):
            nxt = tokens[prefix_end + 1]
            if any(_similar(nxt, p) for p in ("plastic", "plastics")):
                return True
    return False


def _is_hdpe(tokens: list[str]) -> bool:
    """HDPE written as one word or spaced out ("HD PE", "H D P E"), the
    common transposition "HPDE", or spelled out as high density
    polyethylene - unless the next word says it is only the bag."""
    n = len(tokens)
    for i in range(n):
        for width in (1, 2, 3, 4):
            if i + width > n:
                break
            joined = "".join(tokens[i:i + width])
            if joined in ("hdpe", "hpde"):
                after = tokens[i + width] if i + width < n else ""
                if after not in _PACKAGING_WORDS:
                    return True
                break
            if len(joined) > 4:
                break
        if (i + 2 < n and _similar(tokens[i], "high") and _similar(tokens[i + 1], "density")
                and any(_similar(tokens[i + 2], p) for p in ("polyethylene", "polythene"))):
            return True
    return False


# Label shown to a reader -> recogniser. Order only matters for the label a
# description matching two of them would report, which none on the live
# data does.
BULK_WEIGHT_MATERIALS = (
    ("Steam coal", _is_coal),
    ("HM plastic", _is_hm_plastic),
    ("HDPE", _is_hdpe),
)


@lru_cache(maxsize=4096)
def bulk_weight_material(description: str) -> str | None:
    """Which weighed-by-the-truckload material a PO line is, or None.
    Cached: the matcher asks once per line per run, and descriptions repeat
    heavily across line items."""
    tokens = _tokens(description)
    for label, matches in BULK_WEIGHT_MATERIALS:
        if matches(tokens):
            return label
    return None


def over_delivery_tolerance_pct(description: str) -> Decimal | None:
    """The over-delivery allowance for this PO line's material, or None when
    it gets none (every material not weighed by the truckload)."""
    return BULK_QTY_OVER_TOLERANCE_PCT if bulk_weight_material(description) else None


def value_within_over_tolerance(ordered, received, tolerance_pct: Decimal, epsilon: Decimal) -> bool:
    """Whether a received money figure is at or above the ordered one and no
    more than `tolerance_pct` over it (plus the matcher's rounding epsilon).
    Used for the Net / Taxable / Final value checks of a line whose quantity
    was already accepted inside the allowance: taking 108 of 100 tonnes at
    the PO rate bills 8% more, and that is the same weighbridge difference,
    not a second problem. A value under the ordered one is not excused."""
    if ordered is None or received is None:
        return False
    ordered, received = Decimal(ordered), Decimal(received)
    if received < ordered:
        return False
    return received - ordered <= abs(ordered) * tolerance_pct / Decimal("100") + epsilon
