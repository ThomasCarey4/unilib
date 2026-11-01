import pytest
from helpers import make_notebook
from unilib.notebook import NotebookError, cell_count, section_at, sections


def test_sections_group_markdown_with_following_code(tmp_path):
    nb = make_notebook(tmp_path / "n.ipynb", [
        ("code", "import numpy as np"),
        ("markdown", "# Newton's method"),
        ("code", "x = newton(f, x0)", [{"output_type": "stream", "name": "stdout", "text": "converged in 5 its\n"}]),
        ("markdown", "## Line search"),
        ("code", "armijo(phi)", [{"output_type": "display_data", "data": {"image/png": "AAAA", "text/plain": "<Figure>"}}]),
    ])
    secs = sections(nb)
    assert [s.cell for s in secs] == [1, 2, 4]
    assert "import numpy" in secs[0].text
    assert "Newton's method" in secs[1].text and "```python\nx = newton(f, x0)\n```" in secs[1].text
    assert "converged in 5 its" in secs[1].text
    assert "AAAA" not in secs[2].text and "<Figure>" in secs[2].text
    assert cell_count(nb) == 5


def test_source_as_list_and_error_output(tmp_path):
    nb = make_notebook(tmp_path / "n.ipynb", [
        ("markdown", ["# Title\n", "more"]),
        ("code", "1/0", [{"output_type": "error", "ename": "ZeroDivisionError", "evalue": "division by zero", "traceback": []}]),
    ])
    text = sections(nb)[0].text
    assert "# Title\nmore" in text and "ZeroDivisionError: division by zero" in text


def test_long_section_splits_at_cell_boundary(tmp_path):
    big = " ".join(["word"] * 250)
    nb = make_notebook(tmp_path / "n.ipynb", [("markdown", "# A"), ("code", big), ("code", big), ("code", "tail")])
    assert [s.cell for s in sections(nb)] == [1, 3]


def test_output_truncated(tmp_path):
    nb = make_notebook(tmp_path / "n.ipynb", [("code", "print", [{"output_type": "stream", "name": "stdout", "text": "x" * 5000}])])
    assert sections(nb)[0].text.count("x") <= 1000 + 10


def test_section_at(tmp_path):
    nb = make_notebook(tmp_path / "n.ipynb", [("markdown", "# A"), ("code", "a"), ("markdown", "# B")])
    assert section_at(nb, 2).cell == 1
    assert section_at(nb, 3).cell == 3
    with pytest.raises(ValueError):
        section_at(nb, 9)


def test_invalid_notebook(tmp_path):
    bad = tmp_path / "bad.ipynb"
    bad.write_text("not json")
    with pytest.raises(NotebookError):
        sections(bad)


def _raw(tmp_path, nb, name="x.ipynb", encoding="utf-8"):
    import json
    p = tmp_path / name
    p.write_text(json.dumps(nb), encoding=encoding)
    return p


@pytest.mark.parametrize("nb", [
    {"cells": [{"cell_type": "code", "source": "x", "outputs": None}, "oops"]},
    {"cells": ["oops"]},
    {"cells": "x"},
    {"cells": [{"cell_type": "code", "source": 5}]},
])
def test_malformed_raises_notebook_error(tmp_path, nb):
    p = _raw(tmp_path, nb)
    with pytest.raises(NotebookError):
        sections(p)
    if nb["cells"] != [{"cell_type": "code", "source": 5}]:
        with pytest.raises(NotebookError):
            cell_count(p)


def test_null_outputs_and_non_dict_outputs_ok(tmp_path):
    p = _raw(tmp_path, {"cells": [{"cell_type": "code", "source": "x=1", "outputs": None},
                                  {"cell_type": "code", "source": "y=2", "outputs": ["junk"]}]})
    assert sections(p)


def test_bom_notebook(tmp_path):
    p = _raw(tmp_path, {"cells": [{"cell_type": "markdown", "source": "hello"}]}, encoding="utf-8-sig")
    assert sections(p)[0].text == "hello"


def test_nbformat3_rejected(tmp_path):
    p = _raw(tmp_path, {"nbformat": 3, "worksheets": [{"cells": []}]})
    with pytest.raises(NotebookError, match="nbformat 3"):
        sections(p)


def test_data_uri_stripped(tmp_path):
    p = _raw(tmp_path, {"cells": [{"cell_type": "markdown", "source": "![a](data:image/png;base64,AAAA) end"}]})
    t = sections(p)[0].text
    assert "AAAA" not in t and "data:…" in t and "end" in t


def test_section_text_capped(tmp_path):
    p = _raw(tmp_path, {"cells": [{"cell_type": "markdown", "source": "a" * 50000}]})
    t = sections(p)[0].text
    assert len(t) <= 20000 + len("\n…[truncated]") and t.endswith("\n…[truncated]")
