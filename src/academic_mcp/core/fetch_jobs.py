"""Coalesced background fetches for batches with a bounded response wait."""

import asyncio
import os
import time

_jobs = {}
_progress = {}
_semaphore = None


def _deadline():
    return float(os.getenv("ARTICLE_FETCH_TIMEOUT_SECONDS", "300"))


def describe(doi):
    """Human-readable state of a running job: queued or running, and for how long."""
    state = _progress.get(doi.lower().strip())
    if not state:
        return ""
    now = time.monotonic()
    if state.get("started") is None:
        return f"queued behind other fetches for {now - state['queued']:.0f}s"
    elapsed = now - state["started"]
    return f"running for {elapsed:.0f}s; gives up after {_deadline():.0f}s"


def get_or_start(doi, factory):
    global _semaphore
    key = doi.lower().strip()
    existing = _jobs.get(key)
    if existing and (not existing[0].done() or time.monotonic() - existing[1] < 30):
        return existing[0]
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(4)

    _progress[key] = {"queued": time.monotonic(), "started": None}

    async def run():
        async with _semaphore:
            _progress[key]["started"] = time.monotonic()
            try:
                async with asyncio.timeout(_deadline()):
                    return await factory()
            except Exception as exc:
                # Store an outcome rather than leaving an unobserved task
                # exception when the client has already received partial data.
                return exc

    task = asyncio.create_task(run())
    _jobs[key] = (task, time.monotonic())

    def finished(_):
        _jobs[key] = (task, time.monotonic())
        for old, (job, stamp) in list(_jobs.items()):
            if job.done() and time.monotonic() - stamp > 600:
                _jobs.pop(old, None)
                _progress.pop(old, None)

    task.add_done_callback(finished)
    return task
