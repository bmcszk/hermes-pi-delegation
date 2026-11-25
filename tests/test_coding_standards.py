import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import lint_lengths  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


# given: the whole repository tree
# when: the length checker runs over it
# then: no function exceeds 100 lines and no file exceeds 1000 lines
def test_whole_repo_respects_length_limits():
    assert lint_lengths.find_violations([REPO_ROOT]) == []
