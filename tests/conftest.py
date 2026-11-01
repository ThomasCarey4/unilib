import pytest

from helpers import KRR, MODULES_TOML, SCI


@pytest.fixture(autouse=True)
def isolate_client(tmp_path, monkeypatch):
    """Never read the real ~/.config/unilib/client.toml or write the real ~/.cache/unilib."""
    monkeypatch.setenv("UNILIB_CLIENT_CONFIG", str(tmp_path / "client.toml"))
    monkeypatch.setenv("UNILIB_CACHE", str(tmp_path / "cache"))


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "UniLibrary"
    root.mkdir()
    (root / "modules.toml").write_text(MODULES_TOML)
    for d in ("_ingest", "_unsorted", SCI, KRR):
        (root / d).mkdir()
    monkeypatch.setenv("UNILIB_ROOT", str(root))
    return root
