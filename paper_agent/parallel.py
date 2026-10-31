"""Wait for the network side by side, without ever letting a thread touch the database or make a decision.

THE RULE that makes this safe: a job is a plain function that takes nothing from the database and returns plain data (a downloaded page, a price). The CALLER does everything else, in the order IT chooses:
decide what to fetch (database reads), run the jobs here, then parse, store and decide (database writes) one result at a time in a fixed order. Results come back in the order the jobs were given,
whichever finished first, so a decision can never depend on which server happened to answer fastest.

Politeness and limits: at most `per_host` jobs per server at once, an optional minimum gap between two requests to the same server (GDELT asks for one per 5 seconds), and a hard overall deadline after which
unfinished jobs are reported as timed out instead of holding up the cycle. One job failing never affects another. workers=1 runs everything in the calling thread, one after another (the old behaviour).
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, List, Optional


class Outcome:
    """What happened to one job: ok + value, or an error; how long it took; whether the overall deadline cut it off."""
    __slots__ = ("key", "ok", "value", "error", "seconds", "timed_out")

    def __init__(self, key=None, ok: bool = False, value=None, error: Optional[BaseException] = None, seconds: float = 0.0, timed_out: bool = False):
        self.key, self.ok, self.value, self.error, self.seconds, self.timed_out = key, ok, value, error, seconds, timed_out

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Outcome(%r, ok=%s, %.2fs%s)" % (self.key, self.ok, self.seconds, ", timed out" if self.timed_out else "")


def interleave(hosts: List[str]) -> List[int]:
    """Indexes ordered round-robin across hosts, so 19 pages from one site cannot occupy every worker while other sites wait."""
    lanes: "OrderedDict[str, List[int]]" = OrderedDict()
    for i, h in enumerate(hosts):
        lanes.setdefault(h, []).append(i)
    out: List[int] = []
    while any(lanes.values()):
        for h in list(lanes):
            if lanes[h]:
                out.append(lanes[h].pop(0))
    return out


def run_jobs(jobs: List[dict], workers: int = 6, per_host: int = 2, min_gap: Optional[Dict[str, float]] = None, deadline_s: Optional[float] = None,
             sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> List[Outcome]:
    """jobs: [{"key": anything, "host": "server name", "fn": callable taking no arguments}]. Returns one Outcome per job, IN THE ORDER GIVEN."""
    min_gap = min_gap or {}
    results: List[Optional[Outcome]] = [None] * len(jobs)
    t_start = clock()

    def one(i: int) -> Outcome:
        job = jobs[i]
        t0 = clock()
        try:
            return Outcome(job.get("key"), True, job["fn"](), None, clock() - t0)
        except Exception as e:  # noqa: BLE001 - a job's failure is data for the caller, never an exception here
            return Outcome(job.get("key"), False, None, e, clock() - t0)

    if workers <= 1 or len(jobs) <= 1:
        last_host = None
        seen = set()
        for i, job in enumerate(jobs):
            if deadline_s is not None and clock() - t_start > deadline_s:
                results[i] = Outcome(job.get("key"), False, None, TimeoutError("overall deadline reached before this job started"), 0.0, True)
                continue
            h = job.get("host") or ""
            if h in min_gap and h in seen:
                sleep(min_gap[h])                                   # the old sequential behaviour: a full gap between two requests to the same server
            seen.add(h)
            results[i] = one(i)
        return results  # type: ignore[return-value]

    gates: Dict[str, threading.Semaphore] = {}
    last_start: Dict[str, float] = {}
    gap_lock = threading.Lock()
    for job in jobs:
        gates.setdefault(job.get("host") or "", threading.Semaphore(max(1, per_host)))

    def guarded(i: int) -> None:
        h = jobs[i].get("host") or ""
        with gates[h]:
            gap = min_gap.get(h, 0.0)
            if gap:
                with gap_lock:                                       # serialise only the "when may I start" decision, never the request itself
                    wait = last_start.get(h, -1e9) + gap - clock()
                    last_start[h] = max(clock(), last_start.get(h, -1e9) + gap) if wait > 0 else clock()
                if wait > 0:
                    sleep(wait)
            results[i] = one(i)

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs))))
    futs = [ex.submit(guarded, i) for i in interleave([j.get("host") or "" for j in jobs])]
    done, not_done = concurrent.futures.wait(futs, timeout=deadline_s)
    if not_done:
        ex.shutdown(wait=False, cancel_futures=True)                  # nothing more is started; a request already in flight ends by its own timeout and its result is ignored
    else:
        ex.shutdown(wait=True)
    for i, job in enumerate(jobs):
        if results[i] is None:
            results[i] = Outcome(job.get("key"), False, None, TimeoutError("overall deadline reached"), clock() - t_start, True)
    return results  # type: ignore[return-value]
