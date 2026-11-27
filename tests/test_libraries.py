"""Several libraries served by one server: UNILIB_LIBRARIES, the per-request root, [library] settings."""
import threading
from pathlib import Path

import pytest

from unilib.config import (
    Library,
    LibrarySettings,
    libraries,
    library_root,
    load_library_settings,
    load_modules,
    using_library,
)


def test_libraries_default_is_unilib_root(lib, monkeypatch):
    monkeypatch.delenv("UNILIB_LIBRARIES", raising=False)
    assert libraries() == [Library("uni", lib)]


def test_libraries_from_env(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.setenv("UNILIB_LIBRARIES", f"uni={tmp_path / 'a'}, eng={tmp_path / 'b'}")
    assert libraries() == [Library("uni", (tmp_path / "a").resolve()), Library("eng", (tmp_path / "b").resolve())]


def test_libraries_expands_user(monkeypatch):
    monkeypatch.setenv("UNILIB_LIBRARIES", "uni=~/X")
    assert libraries() == [Library("uni", (Path.home() / "X").resolve())]


@pytest.mark.parametrize("value, match", [
    ("uni", "name=path"),
    ("uni=/a,uni=/b", "duplicate"),
    ("Uni Lib=/a", "name"),
    ("all=/a", "reserved"),
    ("=/a", "name"),
    ("uni=", "path"),
])
def test_libraries_rejects_bad_env(monkeypatch, value, match):
    monkeypatch.setenv("UNILIB_LIBRARIES", value)
    with pytest.raises(ValueError, match=match):
        libraries()


def test_using_library_overrides_root(lib, tmp_path):
    other = tmp_path / "Other"
    other.mkdir()
    with using_library(other):
        assert library_root() == other.resolve()
    assert library_root() == lib


def test_using_library_is_per_thread_context(lib, tmp_path):
    other = tmp_path / "Other"
    other.mkdir()
    seen = []
    with using_library(other):
        t = threading.Thread(target=lambda: seen.append(library_root()))
        t.start()
        t.join()
    assert seen == [lib]  # a plain new thread does not inherit the override (anyio copies the context explicitly)


def test_library_settings_default(lib):
    assert load_library_settings(lib) == LibrarySettings()


def test_library_settings_from_toml(lib):
    text = (lib / "modules.toml").read_text()
    (lib / "modules.toml").write_text('[library]\ntitle = "Engineering references"\nbooks_default = true\n' + text)
    assert load_library_settings(lib) == LibrarySettings(title="Engineering references", books_default=True)


def test_library_settings_rejects_non_bool(lib):
    text = (lib / "modules.toml").read_text()
    (lib / "modules.toml").write_text('[library]\nbooks_default = "yes"\n' + text)
    with pytest.raises(ValueError, match="books_default"):
        load_library_settings(lib)


def test_module_semester_optional(tmp_path):
    (tmp_path / "modules.toml").write_text('[[module]]\ncode = "PG"\nname = "PostgreSQL"\nfolder = "PG"\n')
    assert load_modules(tmp_path)[0].semester is None
