import pytest

from unilib.organize import build_name, kebab, parse_name, sha256, unique_dest


def test_kebab():
    assert kebab("newton methods: line search") == "Newton-Methods-Line-Search"
    assert kebab("BFGS and PDE intro") == "BFGS-And-PDE-Intro"


def test_build_name():
    assert build_name("L", 3, "Newton methods") == "L03_Newton-Methods.pdf"
    assert build_name("CW", 1, "Assessment brief") == "CW01_Assessment-Brief.pdf"
    assert build_name("X", None, "Module handbook") == "X_Module-Handbook.pdf"
    with pytest.raises(ValueError):
        build_name("Q", 1, "x")


def test_parse_name_roundtrip():
    assert parse_name("L03_Newton-Methods.pdf") == ("L", 3, "Newton Methods")
    assert parse_name("CW01_Assessment-Brief.pdf") == ("CW", 1, "Assessment Brief")
    assert parse_name("X_Module-Handbook.pdf") == ("X", None, "Module Handbook")
    assert parse_name("random slides.pdf") == ("X", None, "random slides")


def test_unique_dest(tmp_path):
    assert unique_dest(tmp_path, "a.pdf") == tmp_path / "a.pdf"
    (tmp_path / "a.pdf").write_bytes(b"1")
    assert unique_dest(tmp_path, "a.pdf") == tmp_path / "a-b.pdf"
    (tmp_path / "a-b.pdf").write_bytes(b"2")
    assert unique_dest(tmp_path, "a.pdf") == tmp_path / "a-c.pdf"


def test_sha256(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"abc")
    assert sha256(f).startswith("ba7816bf")


def test_kebab_transliterates():
    assert kebab("Übung Ökonomie") == "Ubung-Okonomie"


def test_build_name_rejects_bad_num():
    for bad in (-1, 100, 1000):
        with pytest.raises(ValueError):
            build_name("L", bad, "x")
    assert build_name("L", 0, "x") == "L00_X.pdf"
    assert build_name("L", 99, "x") == "L99_X.pdf"


def test_build_name_rejects_empty_title():
    for bad in ("", "!!!", "日本語"):
        with pytest.raises(ValueError):
            build_name("L", 1, bad)


def test_parse_name_strips_collision_suffix():
    assert parse_name("L01_Intro-b.pdf") == ("L", 1, "Intro")
    assert parse_name("L01_Intro-z.pdf") == ("L", 1, "Intro")


@pytest.mark.parametrize("t,n,title,expected", [
    ("L", 3, "Newton methods", "Newton Methods"),
    ("CW", None, "Assessment brief", "Assessment Brief"),
    ("X", 0, "Übung Ökonomie", "Ubung Okonomie"),
    ("T", 99, "BFGS and PDE", "BFGS And PDE"),
    ("N", 12, "a", "A"),
])
def test_name_roundtrip(t, n, title, expected):
    assert parse_name(build_name(t, n, title)) == (t, n, expected)


def test_unique_dest_exhausted(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"1")
    for c in "bcdefghijklmnopqrstuvwxyz":
        (tmp_path / f"a-{c}.pdf").write_bytes(b"1")
    with pytest.raises(RuntimeError, match="too many name collisions"):
        unique_dest(tmp_path, "a.pdf")


def test_notebook_names():
    assert build_name("E", 9, "Line search demo", ext=".ipynb") == "E09_Line-Search-Demo.ipynb"
    assert parse_name("E09_Line-Search-Demo.ipynb") == ("E", 9, "Line Search Demo")
    assert parse_name("L03_X.pdf") == ("L", 3, "X")
    with pytest.raises(ValueError):
        build_name("L", 1, "x", ext=".txt")


def test_build_name_e_is_notebook_only():
    import pytest
    from unilib.organize import build_name
    with pytest.raises(ValueError):
        build_name("E", 1, "Demo", ext=".pdf")
    assert build_name("N", 1, "Demo", ext=".ipynb") == "N01_Demo.ipynb"


def test_book_part_names():
    assert (build_name("B", 3, "Burden Ch3 Interpolation Polynomial Approximation")
            == "B03_Burden-Ch3-Interpolation-Polynomial-Approximation.pdf")
    assert parse_name("B03_Burden-Ch3-Interpolation.pdf") == ("B", 3, "Burden Ch3 Interpolation")
    with pytest.raises(ValueError, match="PDF-only"):
        build_name("B", 1, "Burden Ch1", ext=".ipynb")
