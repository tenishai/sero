"""Every CI job a fork's pull request could land on a self-hosted runner keeps its
fork guard (go-public stage 2). GitHub reads a pull_request workflow from the PR's
own ref, so the guard is what keeps a stranger's job off our runner until a
maintainer approves it; a job that loses the guard fails this test, not the runner."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GUARD = "github.event.pull_request.head.repo.full_name"


def _triggers(doc: dict) -> set[str]:
    on = doc.get("on", doc.get(True, {}))  # PyYAML reads a bare `on:` key as True
    if isinstance(on, dict):
        return set(on)
    if isinstance(on, list):
        return set(on)
    return {str(on)}


def test_every_self_hosted_pull_request_job_keeps_its_fork_guard() -> None:
    guarded: list[str] = []
    missing: list[str] = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        doc = yaml.safe_load(workflow.read_text())
        if not ({"pull_request", "pull_request_target"} & _triggers(doc)):
            continue  # a push-only or manual workflow never runs a fork's code
        for name, job in (doc.get("jobs") or {}).items():
            if "vars." not in str(job.get("runs-on", "")):
                continue  # a hosted runner: the fork gets GitHub's sandbox, not ours
            bucket = guarded if GUARD in str(job.get("if", "")) else missing
            bucket.append(f"{workflow.name}:{name}")
    assert not missing, f"self-hosted jobs a fork PR could reach without a guard: {missing}"
    assert guarded, "no guarded job found: the workflow layout changed, update this test"
