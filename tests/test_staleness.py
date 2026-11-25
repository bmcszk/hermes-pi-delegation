import pathlib

import __init__ as p

SCRIPTS_FILE = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "registry_log.py"


def test_stale_flips_when_a_scripts_file_changes():
    """A change under scripts/ (not just __init__.py) must flip staleness."""
    # given: the loaded digest matches disk, so the plugin reads fresh
    health = p._plugin_health()
    assert health["stale"] is False, health

    original = SCRIPTS_FILE.read_text()
    try:
        SCRIPTS_FILE.write_text(original + "\n# staleness probe\n")
        # when
        health = p._plugin_health()
        # then
        assert health["stale"] is True, health
        assert health["loaded_sha"] != health["disk_sha"]
    finally:
        SCRIPTS_FILE.write_text(original)
