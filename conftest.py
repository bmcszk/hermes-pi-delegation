"""Make the plugin under test importable without hardcoding any checkout path."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

for entry in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)
