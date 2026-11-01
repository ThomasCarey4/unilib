"""Fold math notation in extracted slide text to the ASCII words people type.

Slides extract as `J(xk)δk = −F(xk)`; queries are typed as `J(x) delta = -F(x)`.
`fold` maps both to the same keyword form. It works one character at a time, so
`fold_with_offsets` can map positions in the folded text back to the source.
"""
from __future__ import annotations

import unicodedata

_GREEK_NAMES = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho "
                "sigma tau upsilon phi chi psi omega").split()
_GREEK_LOWER = "αβγδεζηθικλμνξοπρστυφχψω"
_GREEK_UPPER = "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"

_MAP: dict[str, str] = {}
for _name, _lo, _up in zip(_GREEK_NAMES, _GREEK_LOWER, _GREEK_UPPER):
    _MAP[_lo] = f" {_name} "
    _MAP[_up] = f" {_name.capitalize()} "
_MAP.update({
    # Greek variant forms used in maths typesetting
    "ϕ": " phi ", "ϵ": " epsilon ", "ϑ": " theta ", "ϖ": " pi ", "ϱ": " rho ", "ς": " sigma ",
    "∇": " nabla grad ", "∂": " partial ", "∑": " sum ", "∫": " integral ",
    "−": "-", "–": "-", "—": "-", "′": "'", "×": " x ",
    "≤": " <= ", "≥": " >= ", "≈": " approx ",
    # standalone spacing accents that PDF extraction splits off the letter (`Lindel¨of`)
    "¨": "", "´": "", "˜": "", "˙": "", "ˆ": "", "˘": "", "ˇ": "", "¯": "",
})


def _fold_char(ch: str) -> str:
    if ch.isascii():
        return ch
    mapped = _MAP.get(ch)
    if mapped is not None:
        return mapped
    # NFKD may decompose to a mapped character (Ω OHM SIGN -> Ω, µ MICRO SIGN -> μ)
    return "".join(_MAP.get(c, c) for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))


def fold(text: str) -> str:
    return "".join(_fold_char(ch) for ch in text)


def fold_with_offsets(text: str) -> tuple[str, list[int]]:
    """Folded text plus, for each folded character, the index of the source character it came from."""
    parts: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(text):
        f = _fold_char(ch)
        parts.append(f)
        offsets.extend([i] * len(f))
    return "".join(parts), offsets
