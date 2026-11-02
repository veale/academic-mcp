"""Coalesced background fetches for batches with a bounded response wait."""

import asyncio
import os
import time

_jobs = {}
_semaphore = None


def get_or_start(doi, factory):
    global _semaphore
    key = doi.lower().strip()
    existing = _jobs.get(key)
    if existing and (not existing[0].done() or time.monotonic() - existing[1] < 30):
        return existing[0]
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(4)

    async def run():
        async with _semaphore:
            try:
                async with asyncio.timeout(
                    float(os.getenv("ARTICLE_FETCH_TIMEOUT_SECONDS", "300"))
                ):
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

    task.add_done_callback(finished)
    return task
