from __future__ import annotations

import pytest

from tools.file_edit_tool import (
    FileEditError,
    FileEditTool,
    apply_edits_atomically,
    dry_run_edits,
    parse_search_replace_blocks,
)


def test_parse_single_search_replace_block():
    text = """src/app.py
<<<<<<< SEARCH
def hello():
    return "old"
=======
def hello():
    return "new"
>>>>>>> REPLACE
"""

    edits = parse_search_replace_blocks(text)

    assert len(edits) == 1
    assert edits[0].path == "src/app.py"
    assert edits[0].search == 'def hello():\n    return "old"\n'
    assert edits[0].replace == 'def hello():\n    return "new"\n'


def test_parse_missing_separator_fails():
    text = """src/app.py
<<<<<<< SEARCH
old
>>>>>>> REPLACE
"""

    with pytest.raises(FileEditError, match="missing"):
        parse_search_replace_blocks(text)


def test_dry_run_returns_diff_without_writing(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    edits = parse_search_replace_blocks("""app.py
<<<<<<< SEARCH
value = 1
=======
value = 2
>>>>>>> REPLACE
""")

    plan = dry_run_edits(edits, tmp_path)

    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert "-value = 1" in plan.diff
    assert "+value = 2" in plan.diff


def test_apply_writes_after_successful_plan(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    edits = parse_search_replace_blocks("""app.py
<<<<<<< SEARCH
value = 1
=======
value = 2
>>>>>>> REPLACE
""")
    plan = dry_run_edits(edits, tmp_path)

    result = apply_edits_atomically(plan)

    assert result.files_written == 1
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_search_must_match_exactly_once(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\nvalue = 1\n", encoding="utf-8")
    edits = parse_search_replace_blocks("""app.py
<<<<<<< SEARCH
value = 1
=======
value = 2
>>>>>>> REPLACE
""")

    with pytest.raises(FileEditError, match="matched 2 times"):
        dry_run_edits(edits, tmp_path)


def test_multiple_edits_same_file_apply_in_order(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    edits = parse_search_replace_blocks("""app.py
<<<<<<< SEARCH
a = 1
=======
a = 10
>>>>>>> REPLACE

app.py
<<<<<<< SEARCH
b = 2
=======
b = 20
>>>>>>> REPLACE
""")

    plan = dry_run_edits(edits, tmp_path)
    apply_edits_atomically(plan)

    assert target.read_text(encoding="utf-8") == "a = 10\nb = 20\n"


def test_apply_aborts_if_file_changed_after_dry_run(tmp_path):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("a = 1\n", encoding="utf-8")
    second.write_text("b = 2\n", encoding="utf-8")
    edits = parse_search_replace_blocks("""first.py
<<<<<<< SEARCH
a = 1
=======
a = 10
>>>>>>> REPLACE

second.py
<<<<<<< SEARCH
b = 2
=======
b = 20
>>>>>>> REPLACE
""")
    plan = dry_run_edits(edits, tmp_path)
    second.write_text("b = 200\n", encoding="utf-8")

    with pytest.raises(FileEditError, match="changed after dry-run"):
        apply_edits_atomically(plan)

    assert first.read_text(encoding="utf-8") == "a = 1\n"
    assert second.read_text(encoding="utf-8") == "b = 200\n"


def test_file_edit_tool_dry_run_and_apply(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    tool = FileEditTool(repo_root=tmp_path)
    params = {
        "edits_text": """app.py
<<<<<<< SEARCH
value = 1
=======
value = 2
>>>>>>> REPLACE
""",
        "dry_run": True,
    }

    dry_result = tool.execute(params)
    assert dry_result.success
    assert "Dry-run succeeded" in dry_result.output
    assert target.read_text(encoding="utf-8") == "value = 1\n"

    apply_result = tool.execute({**params, "dry_run": False})
    assert apply_result.success
    assert "Apply succeeded" in apply_result.output
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_path_policy_hook_can_reject_paths(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    edits = parse_search_replace_blocks("""app.py
<<<<<<< SEARCH
value = 1
=======
value = 2
>>>>>>> REPLACE
""")

    with pytest.raises(FileEditError, match="not allowed"):
        dry_run_edits(edits, tmp_path, path_policy=lambda _path: False)


def test_lf_block_can_patch_crlf_file(tmp_path):
    target = tmp_path / "app.py"
    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write("value = 1\r\n")
    edits = parse_search_replace_blocks("""app.py
<<<<<<< SEARCH
value = 1
=======
value = 2
>>>>>>> REPLACE
""")

    plan = dry_run_edits(edits, tmp_path)
    apply_edits_atomically(plan)

    with target.open("r", encoding="utf-8", newline="") as handle:
        assert handle.read() == "value = 2\r\n"
