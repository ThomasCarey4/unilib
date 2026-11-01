import os
import subprocess
import sys

from unilib import models, qwen


def test_make_reranker_is_qwen_and_unloaded():
    assert isinstance(models.make_reranker(), qwen.QwenReranker)  # construction loads nothing


def test_make_embedder_is_qwen(monkeypatch):
    monkeypatch.setattr(qwen, "QwenEmbedder", lambda: "qwen")
    assert models.make_embedder() == "qwen"


def test_import_and_unloaded_reranker_never_import_torch():
    code = (
        "import sys\n"
        "from unilib import models, cli, server\n"
        "models.make_reranker()\n"
        "assert 'torch' not in sys.modules\n"
        "assert 'sentence_transformers' not in sys.modules\n"
        "print('ok')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=os.environ)
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout
