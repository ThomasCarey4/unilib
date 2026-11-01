from pathlib import Path

import pytest

from helpers import add_papers
from unilib.config import (
    PAPERS,
    REPORT_MARKER,
    db_path,
    client_papers,
    client_webdav,
    library_root,
    load_modules,
    load_papers,
    module_by_code,
    paper_file,
    paper_rel,
)


def test_library_root_from_env(lib):
    assert library_root() == lib


def test_load_modules(lib):
    mods = load_modules(lib)
    assert [m.code for m in mods] == ["MATH3001", "COMP3002"]
    assert mods[1].keywords == ("logic",)
    assert mods[0].semester == 1


def test_module_by_code_case_insensitive(lib):
    assert module_by_code(lib, "math3001").folder == "MATH3001_Scientific-Computation"
    with pytest.raises(KeyError):
        module_by_code(lib, "COMP0000")


def test_library_root_empty_env_falls_back(monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("UNILIB_ROOT", "")
    assert library_root() == (Path.home() / "Documents" / "UniLibrary").resolve()


def test_model_cache_dir_empty_env_falls_back(monkeypatch):
    from pathlib import Path

    from unilib.config import model_cache_dir

    monkeypatch.setenv("UNILIB_MODEL_CACHE", "")
    assert model_cache_dir() == Path.home() / ".cache" / "unilib" / "models"


def _write(tmp_path, text):
    (tmp_path / "modules.toml").write_text(text)
    return tmp_path


def test_load_modules_missing_table(tmp_path):
    with pytest.raises(ValueError, match="module"):
        load_modules(_write(tmp_path, 'title = "x"\n'))


def test_load_modules_duplicate_code(tmp_path):
    t = '[[module]]\ncode="A1"\nname="a"\nsemester=1\nfolder="f1"\n[[module]]\ncode="a1"\nname="b"\nsemester=1\nfolder="f2"\n'
    with pytest.raises(ValueError, match="duplicate code"):
        load_modules(_write(tmp_path, t))


def test_load_modules_duplicate_folder(tmp_path):
    t = '[[module]]\ncode="A1"\nname="a"\nsemester=1\nfolder="f"\n[[module]]\ncode="B2"\nname="b"\nsemester=1\nfolder="f"\n'
    with pytest.raises(ValueError, match="duplicate folder"):
        load_modules(_write(tmp_path, t))


def test_db_path(lib):
    assert db_path(lib) == lib / ".index" / "library.db"


def test_load_papers_absent(lib):
    assert load_papers(lib) is None


def test_load_papers_and_report_glob(lib, tmp_path):
    for name in ("dl-cw", "nlp", "bad name", "unmarked"):
        (tmp_path / "latex" / name).mkdir(parents=True)
        (tmp_path / "latex" / name / "refs.bib").write_text("")
        if name != "unmarked":  # a refs.bib alone is not a report: sync-report opts the folder in
            (tmp_path / "latex" / name / REPORT_MARKER).write_text("")
    (tmp_path / "latex" / "empty").mkdir()  # no refs.bib: not a report
    (tmp_path / "latex" / "empty" / REPORT_MARKER).write_text("")
    add_papers(lib, tmp_path / "z" / "uni.bib", f"{tmp_path}/latex/*/refs.bib")
    p = load_papers(lib)
    assert p.bib == tmp_path / "z" / "uni.bib" and p.zotero_dir == tmp_path / "z"
    assert [(r.name, r.bib) for r in p.reports()] == [
        ("dl-cw", tmp_path / "latex" / "dl-cw" / "refs.bib"), ("nlp", tmp_path / "latex" / "nlp" / "refs.bib")]
    assert len(load_modules(lib)) == 2  # [papers] does not disturb [[module]]


def test_load_papers_defaults(lib):
    with open(lib / "modules.toml", "a") as f:
        f.write('\n[papers]\nbib = "~/Zotero/exports/uni.bib"\n')
    p = load_papers(lib)
    assert p.zotero_dir == Path("~/Zotero").expanduser() and p.reports() == ()


def test_load_papers_needs_bib(lib):
    with open(lib / "modules.toml", "a") as f:
        f.write("\n[papers]\n")
    with pytest.raises(ValueError, match=r"\[papers\] needs bib"):
        load_papers(lib)


def test_client_papers_defaults_without_config(tmp_path):
    p = client_papers(tmp_path / "absent.toml")
    assert p.bib == Path("~/Zotero/exports/uni.bib").expanduser()
    assert p.reports_glob == "~/Documents/LaTeX/*/refs.bib"


def test_client_papers_and_webdav_from_config(tmp_path):
    cfg = tmp_path / "client.toml"
    cfg.write_text('[papers]\nbib = "~/z/uni.bib"\n\n'
                   '[webdav]\nurl = "http://host:8091/"\nuser = "unilib"\npassword_file = "~/pw"\n')
    p = client_papers(cfg)
    assert p.bib == Path("~/z/uni.bib").expanduser() and p.reports_glob == "~/Documents/LaTeX/*/refs.bib"
    w = client_webdav(cfg)
    assert (w.url, w.user, w.password_file) == ("http://host:8091", "unilib", Path("~/pw").expanduser())


def test_client_webdav_needs_all_keys(tmp_path):
    cfg = tmp_path / "client.toml"
    cfg.write_text('[webdav]\nurl = "http://host:8091"\n')
    with pytest.raises(ValueError, match=r"\[webdav\] needs url, user and password_file"):
        client_webdav(cfg)


def test_paper_rel_and_file_roundtrip(lib, tmp_path):
    add_papers(lib, tmp_path / "z" / "uni.bib")
    p = load_papers(lib)
    inside, outside = tmp_path / "z" / "storage" / "AB12" / "x.pdf", tmp_path / "linked" / "y.pdf"
    assert paper_rel(p, inside) == "zotero:storage/AB12/x.pdf"
    assert paper_rel(p, outside) == str(outside.resolve())
    assert paper_file(p, "zotero:storage/AB12/x.pdf") == tmp_path / "z" / "storage" / "AB12" / "x.pdf"
    assert paper_file(p, str(outside)) == outside


def test_papers_code_constant():
    assert PAPERS == "PAPERS"
