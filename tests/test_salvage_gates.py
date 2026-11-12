"""salvage_report's quick gates follow the RECORDED cwd's project type.

A worker in a Go repo returned the plugin's own gates (`uvx ruff check .`,
`python -m compileall -q __init__.py scripts tests`), surfacing 23 pre-existing
lint errors from an unrelated project. The gates must judge the tree the worker
actually worked in, and an unrelated repo's errors must not leak in.
"""

import json
import os

from helpers import _parse, _setup_registry, _teardown_registry, _write_registry

import __init__ as p


def _row(cwd, session="epicGates-coder"):
    _write_registry(
        {
            "session": session,
            "pid": 999999,
            "status": "killed",
            "cwd": cwd,
            "started": "2024-01-01T00:00:00",
        }
    )
    return _parse(p._pi_manage_impl("salvage_report", session_id=session))


def _project_types(gates):
    return {row["name"] for row in gates}


def test_go_project_gates_are_gofmt_and_build(tmp_path):
    """go.mod in the recorded cwd: gofmt + `go build ./...`, no plugin checks."""
    repo = tmp_path / "go-repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
    (repo / "main.go").write_text("package main\n\nfunc main() {}\n")
    _setup_registry(str(tmp_path))
    try:
        result = _row(str(repo))
        # then the gate set is the Go quick gate, not the plugin's
        assert result["project_type"] == "go", result
        names = _project_types(result["gates"])
        assert names == {"gofmt", "go-build"}, result["gates"]
        commands = {row["name"]: row["command"] for row in result["gates"]}
        assert "gofmt" in commands["gofmt"], commands
        assert commands["go-build"] == "go build ./...", commands
        # then no unrelated repo's linter runs here
        assert not any("ruff" in row["command"] for row in result["gates"]), result["gates"]
        assert not any("compileall" in row["command"] for row in result["gates"]), result["gates"]
    finally:
        _teardown_registry()


def test_go_project_unformatted_file_fails_gofmt(tmp_path):
    """A Go repo with an unformatted file reports the failure, in that repo."""
    repo = tmp_path / "go-dirty"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/dirty\n\ngo 1.21\n")
    (repo / "main.go").write_text("package main\nfunc main()   {}\n")
    _setup_registry(str(tmp_path))
    try:
        result = _row(str(repo))
        gofmt = next(r for r in result["gates"] if r["name"] == "gofmt")
        # then the row reports the recorded repo's formatting failure
        assert gofmt["ok"] is False, gofmt
        assert "main.go" in gofmt["output_tail"], gofmt
    finally:
        _teardown_registry()


def test_python_project_gates_are_ruff_and_compileall(tmp_path):
    """pyproject.toml in the recorded cwd: ruff + compileall over that tree only."""
    repo = tmp_path / "py-repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'worker'\n")
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("VALUE = 1\n")
    _setup_registry(str(tmp_path))
    try:
        result = _row(str(repo))
        # then
        assert result["project_type"] == "python", result
        names = _project_types(result["gates"])
        assert names == {"ruff-check", "compileall"}, result["gates"]
        commands = {row["name"]: row["command"] for row in result["gates"]}
        assert "ruff check" in commands["ruff-check"], commands
        assert "compileall" in commands["compileall"], commands
        # then the compileall target is the worker's tree, never the plugin's
        assert "__init__.py scripts tests" not in commands["compileall"], commands
    finally:
        _teardown_registry()


def test_python_project_broken_file_fails_compileall(tmp_path):
    """A broken module in the worker repo is reported by the worker repo's gate."""
    repo = tmp_path / "py-broken"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'worker'\n")
    (repo / "broken.py").write_text("def broken(:\n")
    _setup_registry(str(tmp_path))
    try:
        result = _row(str(repo))
        compile_row = next(r for r in result["gates"] if r["name"] == "compileall")
        # then
        assert compile_row["ok"] is False, compile_row
        assert "broken" in compile_row["output_tail"], compile_row
    finally:
        _teardown_registry()


def test_plugin_repo_keeps_the_plugin_gates(tmp_path):
    """plugin.yaml in the recorded cwd: the plugin's own two gates, unchanged."""
    repo = tmp_path / "plugin-repo"
    repo.mkdir()
    (repo / "plugin.yaml").write_text("name: demo\n")
    (repo / "__init__.py").write_text("VALUE = 1\n")
    _setup_registry(str(tmp_path))
    try:
        result = _row(str(repo))
        # then
        assert result["project_type"] == "plugin", result
        assert _project_types(result["gates"]) == {"uvx-ruff-check", "compileall-plugin"}, result
    finally:
        _teardown_registry()


def test_unknown_project_type_reports_a_named_skip(tmp_path):
    """A cwd with no project marker: one named skip, never an unrelated repo's checks."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "notes.md").write_text("nothing to gate here\n")
    _setup_registry(str(tmp_path))
    try:
        result = _row(str(plain))
        # then no gate ran and the reason is named
        assert result["project_type"] == "unknown", result
        assert len(result["gates"]) == 1, result["gates"]
        row = result["gates"][0]
        assert row["skipped"], row
        assert "marker" in row["skipped"] or "go.mod" in row["skipped"], row
    finally:
        _teardown_registry()


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
    _setup_registry(str(tmp_path))
    old_cwd = os.getcwd()
    os.chdir(str(plugin_like))
    try:
        result = _row(str(repo))
        # then the plugin tree's own gates never ran for this row
        names = _project_types(result["gates"])
        assert names == {"gofmt", "go-build"}, result["gates"]
        payload = json.dumps(result["gates"])
        assert "ruff" not in payload, payload
        assert "compileall" not in payload, payload
        assert "plugin-tree" not in payload, payload
    finally:
        os.chdir(old_cwd)
        _teardown_registry()


def test_go_gates_run_inside_the_recorded_cwd(tmp_path):
    """A Go build error lives in the recorded repo, and only there."""
    repo = tmp_path / "go-broken"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/broken\n\ngo 1.21\n")
    (repo / "main.go").write_text("package main\n\nfunc main() { undefinedThing() }\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _setup_registry(str(tmp_path))
    old_cwd = os.getcwd()
    os.chdir(str(elsewhere))
    try:
        result = _row(str(repo))
        build = next(r for r in result["gates"] if r["name"] == "go-build")
        # then the failing build is the recorded repo's, reported from its cwd
        assert build["ok"] is False, build
        assert "undefinedThing" in build["output_tail"], build
    finally:
        os.chdir(old_cwd)
        _teardown_registry()


def test_go_test_gate_stays_behind_a_flag(tmp_path):
    """`go test` dominates runtime, so it never runs unless asked for."""
    repo = tmp_path / "go-tests"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/t\n\ngo 1.21\n")
    (repo / "main.go").write_text("package main\n\nfunc main() {}\n")
    _setup_registry(str(tmp_path))
    try:
        quiet = _row(str(repo))
        # then
        assert "go-test" not in _project_types(quiet["gates"]), quiet["gates"]

        loud = _parse(
            p._pi_manage_impl("salvage_report", session_id="epicGates-coder", go_tests=True)
        )
        assert "go-test" in _project_types(loud["gates"]), loud["gates"]
        test_row = next(r for r in loud["gates"] if r["name"] == "go-test")
        assert test_row["command"].startswith("go test"), test_row
    finally:
        _teardown_registry()


def test_go_mod_under_cwd_wins_over_a_nested_one(tmp_path):
    """Detection reads the recorded cwd's root markers, not a deep descendant."""
    repo = tmp_path / "mixed"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    nested = repo / "vendor" / "go-lib"
    nested.mkdir(parents=True)
    (nested / "go.mod").write_text("module example.com/v\n\ngo 1.21\n")
    _setup_registry(str(tmp_path))
    try:
        result = _row(str(repo))
        # then the root marker decides
        assert result["project_type"] == "python", result
    finally:
        _teardown_registry()


def test_readme_documents_per_project_gate_report():
    readme_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md"
    )
    with open(readme_path) as f:
        readme = f.read()

    # then the per-type report is documented
    assert "go build ./..." in readme
    assert "gofmt" in readme
    assert "salvage_report" in readme
