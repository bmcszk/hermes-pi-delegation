"""salvage_report's quick gates follow the RECORDED cwd's project type.

A worker in a Go repo returned the plugin's own gates (`uvx ruff check .`,
`python -m compileall -q __init__.py scripts tests`), surfacing 23 pre-existing
lint errors from an unrelated project. The gates must judge the tree the worker
actually worked in, and an unrelated repo's errors must not leak in.
"""

import json
import os

import pytest
from helpers import _parse, _setup_registry, _teardown_registry, _write_registry

import __init__ as p


def _row(cwd, session="epicGates-coder", keep_registry=False, **report_over):
    """One registry row + one salvage_report against it. keep_registry=True
    leaves the registry wired for a follow-up report call in the same test."""
    _setup_registry(str(cwd))
    _write_registry(
        {
            "session": session,
            "pid": 999999,
            "status": "killed",
            "cwd": cwd,
            "started": "2024-01-01T00:00:00",
        }
    )
    try:
        return _parse(p._pi_manage_impl("salvage_report", session_id=session, **report_over))
    finally:
        if not keep_registry:
            _teardown_registry()


def _project_types(gates):
    return {row["name"] for row in gates}


@pytest.mark.parametrize(
    ("marker", "expect_type", "expect_gates"),
    [
        pytest.param("go", "go", {"gofmt", "go-build"}, id="go-project"),
        pytest.param("python", "python", {"ruff-check", "compileall"}, id="python-project"),
        pytest.param("unknown", "unknown", {"project-type"}, id="markerless-skip"),
    ],
)
def test_project_marker_selects_the_gate_set(marker, expect_type, expect_gates, tmp_path):
    repo = tmp_path / f"{marker}-repo"
    repo.mkdir()
    if marker == "go":
        (repo / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
        (repo / "main.go").write_text("package main\n\nfunc main() {}\n")
    elif marker == "python":
        (repo / "pyproject.toml").write_text("[project]\nname = 'worker'\n")
    elif marker == "plugin":
        (repo / "plugin.yaml").write_text("name: demo\n")
        (repo / "__init__.py").write_text("VALUE = 1\n")
    else:
        (repo / "notes.md").write_text("nothing to gate here\n")
    result = _row(str(repo))
    assert result["project_type"] == expect_type, result
    assert _project_types(result["gates"]) == expect_gates, result["gates"]
    if marker == "unknown":
        assert result["gates"][0]["skipped"], result["gates"]
        assert (
            "marker" in result["gates"][0]["skipped"] or "go.mod" in result["gates"][0]["skipped"]
        ), result["gates"]
    elif marker == "go":
        # `go build` is named verbatim; `go test` stays behind a flag
        commands = {row["name"]: row["command"] for row in result["gates"]}
        assert commands["go-build"] == "go build ./...", commands
        assert "go-test" not in commands, commands
        loud = _row(str(repo), keep_registry=True, go_tests=True)
        _teardown_registry()
        test_row = next(r for r in loud["gates"] if r["name"] == "go-test")
        assert test_row["command"].startswith("go test"), test_row
    elif marker == "python":
        # the compileall target is the worker's tree, never the plugin's
        commands = {row["name"]: row["command"] for row in result["gates"]}
        assert "__init__.py scripts tests" not in commands["compileall"], commands
        assert "ruff check" in commands["ruff-check"], commands
    else:
        # plugin tree: ruff/compileall never named
        assert not any("ruff" in r["command"] for r in result["gates"]), result["gates"]

    # the per-type report is documented where salvage_report is
    readme_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md"
    )
    with open(readme_path) as f:
        readme = f.read()
    assert "go build ./..." in readme
    assert "gofmt" in readme
    assert "salvage_report" in readme


@pytest.mark.parametrize(
    ("marker", "gate_name", "broken_file", "broken_text"),
    [
        pytest.param(
            "go", "gofmt", "main.go", "package main\nfunc main()   {}\n", id="go-unformatted"
        ),
        pytest.param("python", "compileall", "broken.py", "def broken(:\n", id="python-broken"),
        pytest.param(
            "go",
            "go-build",
            "main.go",
            "package main\n\nfunc main() { undefinedThing() }\n",
            id="go-build-error",
        ),
    ],
)
def test_broken_file_fails_the_recorded_repo_s_gate(
    marker, gate_name, broken_file, broken_text, tmp_path
):
    repo = tmp_path / f"{marker}-dirty"
    repo.mkdir()
    if marker == "go":
        (repo / "go.mod").write_text("module example.com/dirty\n\ngo 1.21\n")
    else:
        (repo / "pyproject.toml").write_text("[project]\nname = 'worker'\n")
    (repo / broken_file).write_text(broken_text)
    # the caller's cwd is outside the row's repo: the gate runs in the recorded cwd
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    old_cwd = os.getcwd()
    os.chdir(str(elsewhere))
    try:
        result = _row(str(repo))
    finally:
        os.chdir(old_cwd)
    gate = next(r for r in result["gates"] if r["name"] == gate_name)
    assert gate["ok"] is False, gate
    assert broken_file.split(".")[0] in gate["output_tail"], gate


def test_unrelated_plugin_errors_cannot_leak_into_a_go_report(tmp_path):
    """The caller's cwd is the plugin tree with a deliberate lint error; a Go
    row's report must carry no trace of it."""
    plugin_like = tmp_path / "plugin-tree"
    plugin_like.mkdir()
    (plugin_like / "plugin.yaml").write_text("name: demo\n")
    (plugin_like / "__init__.py").write_text("import os  # unused: ruff would flag this\n")
    (plugin_like / "pyproject.toml").write_text("[tool.ruff]\n")
    repo = tmp_path / "go-worker"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/worker\n\ngo 1.21\n")
    (repo / "main.go").write_text("package main\n\nfunc main() {}\n")
    old_cwd = os.getcwd()
    os.chdir(str(plugin_like))
    try:
        result = _row(str(repo))
        assert _project_types(result["gates"]) == {"gofmt", "go-build"}, result["gates"]
        payload = json.dumps(result["gates"])
        assert "ruff" not in payload, payload
        assert "compileall" not in payload, payload
        assert "plugin-tree" not in payload, payload
    finally:
        os.chdir(old_cwd)

    # a plugin.yaml marker keeps the plugin's own gates
    plugin_repo = tmp_path / "plugin-repo"
    plugin_repo.mkdir()
    (plugin_repo / "plugin.yaml").write_text("name: demo\n")
    (plugin_repo / "__init__.py").write_text("VALUE = 1\n")
    result = _row(str(plugin_repo))
    assert result["project_type"] == "plugin", result
    assert _project_types(result["gates"]) == {"uvx-ruff-check", "compileall-plugin"}, result

    # detection reads the recorded cwd's root markers, not a deep descendant
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    (mixed / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    nested = mixed / "vendor" / "go-lib"
    nested.mkdir(parents=True)
    (nested / "go.mod").write_text("module example.com/v\n\ngo 1.21\n")
    result = _row(str(mixed))
    assert result["project_type"] == "python", result
