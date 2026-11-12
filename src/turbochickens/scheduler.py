"""Internal cron scheduler (APScheduler) living inside the daemon (spec 06).

One queue, one admission path, one source of truth (baseline v0.2 §3, §9):
when a cron schedule fires, the callback submits the run through the daemon's
own Unix socket exactly like a CLI `tc run` does — the fire path never touches
the queue directly, so manual and cron runs are indistinguishable downstream.

Schedules are persisted in the ``schedules`` table and re-registered with
APScheduler on every daemon start, so a daemon restart loses no schedule.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter

# job_id, spec_path, spec_hash
FireFn = Callable[[str, str, str], Awaitable[None]]

MISFIRE_GRACE_SEC = 300  # a suspended daemon coalesces missed fires, doesn't burst

# Standard 5-field cron day-of-week: 0 and 7 = Sunday, 1 = Monday, …, 6 = Saturday.
_STD_DOW_NAMES = {0: "sun", 1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat"}
# Any fixed Sunday, used to probe which weekdays a day-of-week field matches.
_REF_SUNDAY = datetime(2026, 11, 15)  # 2026-11-15 is a Sunday


def cron_job_id(job_id: str) -> str:
    return f"cron:{job_id}"


def aps_trigger(expr: str) -> CronTrigger:
    """Build an APScheduler CronTrigger from a *standard* 5-field cron expression.

    APScheduler's own ``from_crontab`` numbers day-of-week 0=Monday..6=Sunday,
    which does not match POSIX cron (0/7=Sunday, 1=Monday) that job specs use —
    ``0 7 * * 1`` must mean Monday 07:00. minute/hour/day/month fields agree
    between the two conventions; the day-of-week field is resolved here with
    croniter into unambiguous day names so APScheduler cannot misread it.
    """
    minute, hour, day, month, dow = expr.split()
    matched: list[str] = []
    for std_dow in range(7):
        ref = _REF_SUNDAY + timedelta(days=std_dow)
        probe = f"0 0 * * {dow}"
        if croniter(probe, ref - timedelta(minutes=1)).get_next(datetime) == ref:
            matched.append(_STD_DOW_NAMES[std_dow])
    day_of_week = ",".join(matched) if matched else "*"
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week)


class CronScheduler:
    """Thin wrapper around APScheduler's AsyncIOScheduler.

    The scheduler runs in the daemon's own event loop; job functions are
    coroutines that call `fire(job_id, spec_path, spec_hash)`.
    """

    def __init__(self, fire: FireFn) -> None:
        self._fire = fire
        self._sched = AsyncIOScheduler()
        self._started = False

    def start(self) -> None:
        """Start the scheduler (call from inside the daemon's running loop)."""
        self._sched.start()
        self._started = True

    def stop(self) -> None:
        if self._started:
            self._sched.shutdown(wait=False)
            self._started = False

    def add(self, job_id: str, spec_path: str, spec_hash: str, expr: str) -> datetime | None:
        """Register (or replace) a job's cron schedule. Returns the next run time."""
        trigger = aps_trigger(expr)
        job = self._sched.add_job(
            self._fire,
            trigger,
            args=[job_id, spec_path, spec_hash],
            id=cron_job_id(job_id),
            name=f"cron for job {job_id}",
            replace_existing=True,
            misfire_grace_time=MISFIRE_GRACE_SEC,
            coalesce=True,
        )
        return job.next_run_time

    def remove(self, job_id: str) -> bool:
        """Remove a job's schedule. Returns False if none was registered."""
        if self._sched.get_job(cron_job_id(job_id)) is None:
            return False
        self._sched.remove_job(cron_job_id(job_id))
        return True

    def next_run(self, job_id: str) -> datetime | None:
        job = self._sched.get_job(cron_job_id(job_id))
        return job.next_run_time if job is not None else None

    def registered(self, job_id: str) -> bool:
        return self._sched.get_job(cron_job_id(job_id)) is not None

    def pending(self) -> list[str]:
        """Job IDs with a registered APScheduler job (for status/tests)."""
        return sorted(
            j.id.removeprefix("cron:") for j in self._sched.get_jobs() if j.id.startswith("cron:")
        )

    def describe(self, job_id: str) -> dict[str, Any] | None:
        job = self._sched.get_job(cron_job_id(job_id))
        if job is None:
            return None
        return {"job_id": job_id, "trigger": str(job.trigger), "next_run": job.next_run_time}
