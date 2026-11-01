import shutil
import subprocess

import pytest

from helpers import SCI, make_pdf
from unilib.config import module_by_code
from unilib.organize import convert_pptx, place


def test_place_moves_and_renames(lib):
    src = make_pdf(lib / "_ingest" / "slides(1).pdf", ["Newton"])
    res = place(lib, src, module_by_code(lib, "MATH3001"), "L03_Newton.pdf")
    assert res.status == "placed"
    assert res.path == lib / SCI / "L03_Newton.pdf"
    assert res.path.exists() and not src.exists()


def test_place_drops_exact_duplicate(lib):
    mod = module_by_code(lib, "MATH3001")
    a = make_pdf(lib / "_ingest" / "a.pdf", ["same"])
    shutil.copy(a, lib / "_ingest" / "a-copy.pdf")
    place(lib, a, mod, "L01_Intro.pdf")
    res = place(lib, lib / "_ingest" / "a-copy.pdf", mod, "L02_Other.pdf")
    assert res.status == "duplicate"
    assert res.path == lib / SCI / "L01_Intro.pdf"
    assert not (lib / "_ingest" / "a-copy.pdf").exists()
    assert not (lib / SCI / "L02_Other.pdf").exists()


def test_place_name_collision_gets_suffix(lib):
    mod = module_by_code(lib, "MATH3001")
    place(lib, make_pdf(lib / "_ingest" / "a.pdf", ["one"]), mod, "L01_Intro.pdf")
    res = place(lib, make_pdf(lib / "_ingest" / "b.pdf", ["two"]), mod, "L01_Intro.pdf")
    assert res.path.name == "L01_Intro-b.pdf"


def test_place_moves_sibling_pptx_to_originals(lib):
    src = make_pdf(lib / "_ingest" / "deck.pdf", ["x"])
    (lib / "_ingest" / "deck.pptx").write_bytes(b"pptx")
    res = place(lib, src, module_by_code(lib, "MATH3001"), "L01_Intro.pdf")
    assert (lib / SCI / "_originals" / "L01_Intro.pptx").exists()
    assert not (lib / "_ingest" / "deck.pptx").exists()
    assert res.status == "placed"


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_convert_pptx_failure_raises(lib):
    missing = lib / "_ingest" / "missing.pptx"  # soffice exits 0 but writes nothing
    with pytest.raises(RuntimeError):
        convert_pptx(missing)


def test_place_rejects_file_already_in_library(lib):
    mod = module_by_code(lib, "MATH3001")
    f = make_pdf(lib / SCI / "L01_Intro.pdf", ["x"])
    with pytest.raises(ValueError, match="already in the library"):
        place(lib, f, mod, "L02_Other.pdf")
    assert f.exists()
    g = make_pdf(lib / SCI / "_originals" / "o.pdf", ["y"])
    with pytest.raises(ValueError, match="already in the library"):
        place(lib, g, mod, "L02_Other.pdf")
    assert g.exists()


def test_find_duplicate_exclude(lib):
    from unilib.organize import find_duplicate, sha256
    f = make_pdf(lib / SCI / "L01_Intro.pdf", ["x"])
    assert find_duplicate(lib, sha256(f)) == f
    assert find_duplicate(lib, sha256(f), exclude=f) is None


def test_place_duplicate_moves_pptx_to_unsorted(lib):
    mod = module_by_code(lib, "MATH3001")
    a = make_pdf(lib / "_ingest" / "a.pdf", ["same"])
    place(lib, a, mod, "L01_Intro.pdf")
    b = lib / "_ingest" / "b.pdf"
    shutil.copy(lib / SCI / "L01_Intro.pdf", b)
    (lib / "_ingest" / "b.pptx").write_bytes(b"pptx")
    res = place(lib, b, mod, "L02_Other.pdf")
    assert res.status == "duplicate"
    assert not b.exists()
    assert not (lib / "_ingest" / "b.pptx").exists()
    assert (lib / "_unsorted" / "b.pptx").read_bytes() == b"pptx"


def test_place_duplicate_pptx_unsorted_created_and_unique(lib):
    shutil.rmtree(lib / "_unsorted")
    mod = module_by_code(lib, "MATH3001")
    place(lib, make_pdf(lib / "_ingest" / "a.pdf", ["same"]), mod, "L01_Intro.pdf")
    for i in range(2):
        b = lib / "_ingest" / "b.pdf"
        shutil.copy(lib / SCI / "L01_Intro.pdf", b)
        (lib / "_ingest" / "b.pptx").write_bytes(b"p%d" % i)
        place(lib, b, mod, "L02_Other.pdf")
    assert (lib / "_unsorted" / "b.pptx").read_bytes() == b"p0"
    assert (lib / "_unsorted" / "b-b.pptx").read_bytes() == b"p1"


@pytest.mark.parametrize("name", ["../x.pdf", "sub/x.pdf", "x.txt", "x"])
def test_place_rejects_bad_name(lib, name):
    src = make_pdf(lib / "_ingest" / "a.pdf", ["x"])
    with pytest.raises(ValueError):
        place(lib, src, module_by_code(lib, "MATH3001"), name)
    assert src.exists()


def test_place_creates_missing_module_folder(lib):
    shutil.rmtree(lib / SCI)
    src = make_pdf(lib / "_ingest" / "a.pdf", ["x"])
    res = place(lib, src, module_by_code(lib, "MATH3001"), "L01_Intro.pdf")
    assert res.path.exists()


def _raiser(exc):
    def run(*a, **k):
        raise exc
    return run


def test_convert_pptx_missing_soffice(lib, monkeypatch):
    pptx = lib / "_ingest" / "d.pptx"
    pptx.write_bytes(b"x")
    monkeypatch.setattr(subprocess, "run", _raiser(FileNotFoundError("soffice")))
    with pytest.raises(RuntimeError, match="soffice failed for d.pptx"):
        convert_pptx(pptx)


def test_convert_pptx_timeout(lib, monkeypatch):
    pptx = lib / "_ingest" / "d.pptx"
    pptx.write_bytes(b"x")
    monkeypatch.setattr(subprocess, "run", _raiser(subprocess.TimeoutExpired("soffice", 300)))
    with pytest.raises(RuntimeError, match="soffice failed for d.pptx"):
        convert_pptx(pptx)


def test_convert_pptx_uses_isolated_profile(lib, monkeypatch):
    pptx = lib / "_ingest" / "d.pptx"
    pptx.write_bytes(b"x")
    seen = {}

    def run(cmd, **k):
        seen["cmd"] = cmd
        raise FileNotFoundError("stop")
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError):
        convert_pptx(pptx)
    assert any(a.startswith("-env:UserInstallation=file://") and a.endswith("/lo-profile") for a in seen["cmd"])


def test_convert_pptx_drops_ld_library_path(lib, monkeypatch):
    # A shell LD_LIBRARY_PATH (set for Python's numpy) loads an incompatible glib into soffice.
    pptx = lib / "_ingest" / "d.pptx"
    pptx.write_bytes(b"x")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/nix/store/x-glib/lib")
    monkeypatch.setenv("HOME", "/home/someone")
    seen = {}

    def run(cmd, **k):
        seen["env"] = k.get("env")
        raise FileNotFoundError("stop")
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError):
        convert_pptx(pptx)
    assert seen["env"] is not None
    assert "LD_LIBRARY_PATH" not in seen["env"]
    assert seen["env"]["HOME"] == "/home/someone"


def test_convert_pptx_refuses_to_overwrite(lib, monkeypatch):
    pptx = lib / "_ingest" / "d.pptx"
    pptx.write_bytes(b"x")
    existing = lib / "_ingest" / "d.pdf"
    existing.write_bytes(b"keep me")
    monkeypatch.setattr(subprocess, "run", _raiser(AssertionError("should not run")))
    with pytest.raises(RuntimeError):
        convert_pptx(pptx)
    assert existing.read_bytes() == b"keep me"


def test_place_moves_notebook(lib):
    from helpers import make_notebook
    nb = make_notebook(lib / "_ingest" / "demo.ipynb", [("markdown", "hi")])
    res = place(lib, nb, module_by_code(lib, "MATH3001"), "E09_Demo.ipynb")
    assert res.status == "placed"
    assert res.path == lib / SCI / "E09_Demo.ipynb"
    assert res.path.exists() and not nb.exists()


def test_place_rejects_suffix_mismatch(lib):
    from helpers import make_notebook
    mod = module_by_code(lib, "MATH3001")
    nb = make_notebook(lib / "_ingest" / "demo.ipynb", [("markdown", "hi")])
    with pytest.raises(ValueError):
        place(lib, nb, mod, "E09_Demo.pdf")
    pdf = make_pdf(lib / "_ingest" / "a.pdf", ["x"])
    with pytest.raises(ValueError):
        place(lib, pdf, mod, "L01_Demo.ipynb")
    assert nb.exists() and pdf.exists()


def test_place_detects_duplicate_notebook(lib):
    from helpers import make_notebook
    mod = module_by_code(lib, "MATH3001")
    a = make_notebook(lib / "_ingest" / "a.ipynb", [("code", "x=1")])
    shutil.copy(a, lib / "_ingest" / "b.ipynb")
    place(lib, a, mod, "E01_A.ipynb")
    res = place(lib, lib / "_ingest" / "b.ipynb", mod, "E02_B.ipynb")
    assert res.status == "duplicate"
    assert res.path == lib / SCI / "E01_A.ipynb"
    assert not (lib / "_ingest" / "b.ipynb").exists()


def test_place_uppercase_extensions(lib):
    from helpers import make_notebook
    mod = module_by_code(lib, "MATH3001")
    pdf = make_pdf(lib / "_ingest" / "Slides.PDF", ["x"])
    nb = make_notebook(lib / "_ingest" / "Demo.IPYNB", [("markdown", "hi")])
    assert place(lib, pdf, mod, "L01_Slides.pdf").path == lib / SCI / "L01_Slides.pdf"
    assert place(lib, nb, mod, "E01_Demo.ipynb").path == lib / SCI / "E01_Demo.ipynb"
    assert (lib / SCI / "L01_Slides.pdf").exists() and (lib / SCI / "E01_Demo.ipynb").exists()


def test_place_notebook_leaves_unrelated_pptx(lib):
    from helpers import make_notebook
    mod = module_by_code(lib, "MATH3001")
    nb = make_notebook(lib / "_ingest" / "pair.ipynb", [("markdown", "hi")])
    pptx = lib / "_ingest" / "pair.pptx"
    pptx.write_bytes(b"pptx")
    place(lib, nb, mod, "E01_Pair.ipynb")
    assert pptx.exists()
    nb2 = make_notebook(lib / "_ingest" / "pair.ipynb", [("markdown", "hi")])
    assert place(lib, nb2, mod, "E02_Pair.ipynb").status == "duplicate"
    assert pptx.exists()
