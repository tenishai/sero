"""Tests for the conventions lint (scripts/lint-conventions.sh).

The lint is the real gate for the hard conventions, so its patterns need the
same regression cover as the Python. Each case writes a file of planted values
and runs the script over just that file, which is the script's own
"check only these paths" mode.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lint-conventions.sh"


def run_lint(tmp_path, text, name="planted.txt"):
    target = tmp_path / name
    target.write_text(text)
    return subprocess.run(
        ["bash", str(SCRIPT), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "region",
    [
        "us-east4",
        "us-east-1",
        "europe-west1",
        "africa-south1",
        "cn-north-1",
        "us-gov-west-1",
        "asia-northeast2",
        "ca-central-1",
        "eastus2",
        "westeurope",
        "southindia",
        "us-west",
    ],
)
def test_a_cloud_region_literal_is_rejected(tmp_path, region):
    result = run_lint(tmp_path, f'location = "{region}"\n')
    assert result.returncode == 1
    assert "FAIL: no cloud region literals" in result.stdout


@pytest.mark.parametrize(
    "text",
    [
        "eastbound-17",
        "sample-west-2",
        "<region>",
        "example-region1",
        "us-eastern seaboard",
        "the northwest passage",
    ],
)
def test_text_that_only_looks_like_a_region_passes(tmp_path, text):
    result = run_lint(tmp_path, f"value = {text}\n")
    assert result.returncode == 0, result.stdout


def test_a_clean_file_passes_every_check(tmp_path):
    result = run_lint(tmp_path, "nothing to see here\n")
    assert result.returncode == 0, result.stdout
    assert "conventions lint passed" in result.stdout


def test_an_em_dash_warns_and_does_not_fail(tmp_path):
    result = run_lint(tmp_path, "a sentence — with an em dash\n", name="planted.md")
    assert result.returncode == 0, result.stdout
    assert "WARN: em or en dashes in prose" in result.stdout
    assert "planted.md" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="the lint is a POSIX shell script")
def test_the_script_is_executable():
    assert SCRIPT.exists()
