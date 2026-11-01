import pytest

from unilib.textnorm import fold, fold_with_offsets


@pytest.mark.parametrize("src, expected", [
    ("δk", "delta k"),
    ("J(xk)δk = −F(xk)", "J(xk) delta k = -F(xk)"),
    ("ϕ(λ)", "phi ( lambda )"),
    ("φ", "phi"),
    ("Δx", "Delta x"),
    ("αβγεθμπρστωξηκν", "alpha beta gamma epsilon theta mu pi rho sigma tau omega xi eta kappa nu"),
    ("∇ϕ", "nabla grad phi"),
    ("∂u/∂x", "partial u/ partial x"),
    ("∑ ∫", "sum integral"),
    ("a≤b≥c≈d", "a <= b >= c approx d"),
    ("n×n", "n x n"),
])
def test_fold(src, expected):
    assert " ".join(fold(src).split()) == expected


def test_greek_case_is_kept():
    assert fold("Δ").strip() == "Delta"
    assert fold("δ").strip() == "delta"


@pytest.mark.parametrize("src", ["Lindel¨of", "Lindelöf", "Lindelöf"])
def test_diacritics_removed(src):
    assert fold(src) == "Lindelof"


def test_dashes_and_prime():
    assert fold("a − b – c — d") == "a - b - c - d"
    assert fold("f ′(x)") == "f '(x)"


def test_ascii_is_unchanged():
    s = "J(x) delta = -F(x), x_{n+1} = x_n - f(x_n)/f'(x_n)"
    assert fold(s) == s


def test_compatibility_forms_are_decomposed():
    assert fold("ﬁnd x²") == "find x2"
    assert fold("\u2126").strip() == "Omega"  # OHM SIGN decomposes to Greek capital omega


def test_offsets_map_folded_chars_to_source():
    src = "aδb"
    folded, offsets = fold_with_offsets(src)
    assert folded == fold(src) == "a delta b"
    assert len(offsets) == len(folded)
    assert offsets[folded.index("delta")] == 1
    assert offsets[folded.index("b")] == 2
