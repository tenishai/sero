"""The Dockerfile pins its Python base image by digest in three FROM lines.
Nothing but this test keeps the three equal, and three stages built from two
different bases is the kind of drift a review does not see."""

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def test_every_python_stage_uses_one_base_digest() -> None:
    froms = re.findall(r"^FROM (python:[^@\s]+@sha256:[0-9a-f]{64})", DOCKERFILE.read_text(), re.M)
    assert len(froms) >= 2, "expected several python FROM lines"
    assert len(set(froms)) == 1, f"python base images disagree: {sorted(set(froms))}"
