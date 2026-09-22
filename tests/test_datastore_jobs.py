"""Tests for the background-job rows the backend records (D14).

A job is operational metadata: who asked for which apply phase, what state it
reached, and the op-by-op report. It never holds a secret value, so these tests
assert the lifecycle and the report round-trip, not any payload shape.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ladon.datastore import repositories as repo
from ladon.datastore.models import Base

ADMIN = "admin@example.com"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_create_job_starts_queued(session):
    job = repo.create_job(session, kind="root-config", requested_by=ADMIN)
    assert job.id is not None
    assert job.state == "queued"
    assert job.requested_by == ADMIN
    assert job.started_at is None
    assert job.finished_at is None


def test_job_runs_then_finishes_with_a_report(session):
    job = repo.create_job(session, kind="team-onboarding", requested_by=ADMIN)
    repo.mark_job_running(session, job_id=job.id)
    assert repo.get_job(session, job.id).state == "running"
    assert repo.get_job(session, job.id).started_at is not None

    done = repo.mark_job_done(session, job_id=job.id, report={"ops": ["already current"]})
    assert done.state == "done"
    assert done.finished_at is not None
    assert done.report_json == {"ops": ["already current"]}
    assert done.error is None


def test_job_failure_records_the_error(session):
    job = repo.create_job(session, kind="app-onboarding", requested_by=ADMIN)
    repo.mark_job_running(session, job_id=job.id)
    failed = repo.mark_job_failed(session, job_id=job.id, error="mount kv not found")

    assert failed.state == "failed"
    assert failed.error == "mount kv not found"
    assert failed.finished_at is not None


def test_get_job_is_none_when_absent(session):
    assert repo.get_job(session, 404) is None


def test_updating_an_unknown_job_raises(session):
    with pytest.raises(KeyError):
        repo.mark_job_running(session, job_id=404)
