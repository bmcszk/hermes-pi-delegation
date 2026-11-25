import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FLAKE8_COMMAND = [
    "uv",
    "run",
    "--with",
    "flake8",
    "--with",
    "flake8-functions",
    "python",
    "-m",
    "flake8",
    "--select=CFQ001",
    "--max-function-length=100",
    ".",
]

PYLINT_COMMAND = [
    "uv",
    "run",
    "--with",
    "pylint",
    "python",
    "-m",
    "pylint",
    "--disable=all",
    "--enable=too-many-lines",
    "--max-module-lines=1000",
    "--score=n",
    "__init__.py",
    "scripts",
    "tests",
]


def run(command):
    return subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)


# given: the whole repository tree
# when: flake8 runs the CFQ001 function-length rule over it
# then: no function exceeds 100 lines
def test_whole_repo_respects_function_length_limit():
    result = run(FLAKE8_COMMAND)
    assert result.returncode == 0, result.stdout + result.stderr


# given: the python sources of the plugin
# when: pylint runs the too-many-lines rule over them
# then: no file exceeds 1000 lines
def test_whole_repo_respects_file_length_limit():
    result = run(PYLINT_COMMAND)
    assert result.returncode == 0, result.stdout + result.stderr
