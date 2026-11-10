"""Preflight model-alias validation and naming mutual exclusion.

Harness mirrors the deployment: the fake models.json/settings.json live in the
isolated worker agent dir (autouse fixture from conftest), exactly where
`_deployment_models` reads them.
"""

import json
import shlex

from helpers import CONTRACT_MD, TMP_CWD, _parse

from __init__ import pi_launch


def _write_deployment(tmp_path, models, default=None):
    """Fake deployment list in the isolated worker agent dir."""
    agent_dir = tmp_path / "agent-worker"
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / "models.json").write_text(
        json.dumps({"providers": {"omni": {"models": [{"id": m} for m in models]}}})
    )
    if default is not None:
        (agent_dir / "settings.json").write_text(json.dumps({"defaultModel": default}))


def _launch(**over):
    payload = {
        "worker_type": "coder",
        "skills": [],
        "contract_path": CONTRACT_MD,
        "cwd": TMP_CWD,
        "epic_id": "epicMdl",
    }
    return _parse(pi_launch({**payload, **over}))


def test_preflight_rejects_unknown_model_alias(tmp_path):
    # given a deployment whose list does not carry the requested alias
    _write_deployment(tmp_path, ["default-alias", "heavy-alias"])
    result = _launch(model="not-an-alias")
    # then preflight fails and names the alias plus valid ids
    assert "error" in result, result
    assert result["error"].startswith("preflight"), result
    assert "not-an-alias" in result["error"], result["error"]
    assert "heavy-alias" in result["error"], result["error"]


def test_preflight_accepts_known_model_alias(tmp_path):
    # given an alias present in the deployment list
    _write_deployment(tmp_path, ["default-alias", "heavy-alias"])
    result = _launch(model="heavy-alias")
    # then the launch succeeds and the alias lands on the worker argv
    assert "error" not in result, result
    parts = shlex.split(result["command"])
    assert parts[parts.index("--model") + 1] == "heavy-alias", parts


def test_preflight_accepts_alias_equal_to_default_model(tmp_path):
    # given an alias absent from models.json but equal to settings.json defaultModel
    _write_deployment(tmp_path, ["default-alias"], default="heavy-alias")
    result = _launch(model="heavy-alias")
    assert "error" not in result, result


def test_preflight_warns_when_model_list_unreadable(tmp_path):
    # given no models.json in the agent dir at all
    result = _launch(model="not-an-alias")
    # then no failure: a warn row degrades the check and the command is emitted
    assert "error" not in result, result
    warnings = result.get("warnings", [])
    assert any(
        "not verifiable against the deployment list" in w and "not-an-alias" in w for w in warnings
    ), warnings
    assert "--model not-an-alias" in " ".join(shlex.split(result["command"]))


def test_preflight_warns_when_model_absent(tmp_path):
    # given no model parameter
    _write_deployment(tmp_path, ["default-alias"], default="heavy-alias")
    result = _launch()
    # then a warn row tells the caller pi's defaultModel will run
    assert "error" not in result, result
    warnings = result.get("warnings", [])
    assert any("defaultModel" in w and "heavy-alias" in w for w in warnings), warnings


def test_preflight_rejects_double_naming(tmp_path):
    # given epic_id and task_slug both set
    result = _launch(epic_id="epicWin", task_slug="slugWin")
    # then preflight fails and names the winner
    assert "error" in result, result
    assert result["error"].startswith("preflight"), result
    assert "epic_id and task_slug are mutually exclusive" in result["error"], result["error"]
    assert "epic_id wins the session id" in result["error"], result["error"]
