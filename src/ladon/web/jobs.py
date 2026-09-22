"""Apply operations as background jobs (D14).

An apply phase reads and writes dozens of objects across OpenBao and the
cluster, which takes longer than a load balancer will hold a request open. So
the endpoint starts a job, answers with its id, and the caller polls the job
row. The row is the record: what was asked for, who asked, when it started and
finished, and the op-by-op report, which names objects and never a value.

Three rules the runner enforces, and why:

* **One job per kind at a time.** Two applies of the same phase converge the
  same objects against each other's half-written state. A second request while
  one is in flight is answered with the running job rather than queued behind
  it, because the caller's next question is "is it still going?".
* **A job runs on its own credential and its own session.** The work function
  builds its own OpenBao client from the deployment's configuration and the
  token in the environment. Nothing from the request survives into the thread,
  so a job cannot end up writing with whatever a request happened to carry.
* **A failure is recorded, not lost.** The thread outlives the request, so a
  traceback has nowhere to go. Whatever the work raises becomes the job's
  error, redacted on its way into the datastore.

The threads are daemons: a job is not a promise that survives the process, and
an operator who restarts Ladon mid-apply re-runs the phase, which converges.
What a restart leaves behind is rows, so ``recover_interrupted`` ends them at
startup rather than leaving a kind claimed by a thread that no longer exists.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from ladon.datastore import repositories as repo
from ladon.datastore.models import Job

log = logging.getLogger(__name__)

# The report lives under one key, so the column can hold what a later phase
# wants to record alongside it without the shape of a report changing.
OPERATIONS = "operations"

# What a job the process did not outlive is recorded as. A restart is not a
# result, but it is an ending, and a row that keeps saying "running" holds its
# kind against every job that comes after it.
INTERRUPTED = "interrupted by a restart"


@dataclass(frozen=True)
class JobOutcome:
    """What a job's work produced: the op-by-op report, and why it did not succeed.

    ``error`` set means the job failed. A blocked operation is one of those: it
    is not an exception (nothing was written, and the report says which object
    and why) but it is not a success either.
    """

    report: tuple[dict[str, Any], ...] = ()
    error: str | None = None


class JobConflict(Exception):
    """A job of this kind is already in flight. Carries the one that is."""

    def __init__(self, kind: str, job_id: int) -> None:
        super().__init__(f"a {kind} job is already running (job {job_id})")
        self.kind = kind
        self.job_id = job_id


# What a job does, and what runs once it has finished and its ending is
# recorded, in a session of its own (so an approval can record the run's error
# on the request it belongs to without being able to undo the ending).
Work = Callable[[], JobOutcome]
OnFinish = Callable[[Session, Job], None]


@dataclass
class JobRunner:
    """Runs apply work in a thread, with the datastore as the record of it."""

    session_factory: sessionmaker[Session]
    # One submit at a time: the check for a running job of the same kind and
    # the insert that claims the kind have to be one step. This holds within
    # one process, which is what a deployment runs (D14); a second replica
    # would need the claim in the database.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _threads: dict[int, threading.Thread] = field(default_factory=dict, repr=False)

    def recover_interrupted(self) -> int:
        """End the jobs a previous process was still running, and say how many.

        Run once at startup, after the datastore is at head. The threads died
        with that process, so the rows are the only thing left of them, and one
        that still says ``running`` would refuse every later job of its kind.
        """
        with self.session_factory() as session:
            swept = repo.fail_unfinished_jobs(session, error=INTERRUPTED)
            session.commit()
        if swept:
            log.warning("ended %s job(s) left unfinished by a previous process", swept)
        return swept

    def submit(
        self,
        *,
        kind: str,
        requested_by: str,
        work: Work,
        on_finish: OnFinish | None = None,
    ) -> int:
        """Record a job, start it, and return its id.

        Raises :class:`JobConflict` when one of this kind has not finished.
        """
        with self._lock:
            with self.session_factory() as session:
                running = repo.active_job(session, kind=kind)
                if running is not None:
                    raise JobConflict(kind, running.id)
                job_id = repo.create_job(session, kind=kind, requested_by=requested_by).id
                session.commit()
            thread = threading.Thread(
                target=self._run,
                args=(job_id, work, on_finish),
                name=f"ladon-job-{job_id}",
                daemon=True,
            )
            self._threads[job_id] = thread
            thread.start()
        return job_id

    def _run(self, job_id: int, work: Work, on_finish: OnFinish | None) -> None:
        with self.session_factory() as session:
            repo.mark_job_running(session, job_id=job_id)
            session.commit()
        try:
            outcome = work()
        except Exception as exc:  # noqa: BLE001 - the thread is the last place to catch it
            # The traceback goes to the log and the message goes to the row,
            # redacted on the way in. Nothing else about the job is logged: an
            # operator reading logs does not need the arguments to find this.
            log.exception("job %s failed", job_id)
            outcome = JobOutcome(error=str(exc))
        self._finish(job_id, outcome, on_finish)

    def _finish(self, job_id: int, outcome: JobOutcome, on_finish: OnFinish | None) -> None:
        """Record how the job ended, then let the hook do its own bookkeeping.

        The two are separate transactions on purpose. The hook belongs to
        whoever submitted the job and can fail on its own terms; sharing a
        transaction with the state write would mean a broken hook rolling the
        job back to ``running``, where it would stay for the life of the
        process and refuse every job of its kind.
        """
        report = {OPERATIONS: list(outcome.report)}
        try:
            with self.session_factory() as session:
                if outcome.error:
                    repo.mark_job_failed(session, job_id=job_id, error=outcome.error, report=report)
                else:
                    repo.mark_job_done(session, job_id=job_id, report=report)
                session.commit()
        except Exception:
            # Nothing above this catches: the request that started the job is
            # long gone. A job stuck in "running" is visible in the listing,
            # which is better than a thread dying silently.
            log.exception("could not record the end of job %s", job_id)
        else:
            self._notify(job_id, on_finish)
        finally:
            with self._lock:
                self._threads.pop(job_id, None)

    def _notify(self, job_id: int, on_finish: OnFinish | None) -> None:
        """Run the submitter's hook over the finished job, in its own session."""
        if on_finish is None:
            return
        try:
            with self.session_factory() as session:
                row = repo.get_job(session, job_id)
                if row is None:  # pragma: no cover - the row was just written
                    return
                on_finish(session, row)
                session.commit()
        except Exception:
            log.exception("the finish hook for job %s failed", job_id)
