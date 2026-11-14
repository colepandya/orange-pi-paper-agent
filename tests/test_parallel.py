"""paper_agent.parallel: network waits side by side, results in input order, per-server limits, deadlines, isolation."""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paper_agent import parallel  # noqa: E402


def job(key, host="a", secs=0.0, value=None, boom=False, log=None):
    def fn():
        if log is not None:
            log.append(("start", key, time.monotonic()))
        time.sleep(secs)
        if boom:
            raise ValueError("boom " + str(key))
        return value if value is not None else key
    return {"key": key, "host": host, "fn": fn}


class TestOrderAndSpeed(unittest.TestCase):
    def test_results_come_back_in_the_order_given_whichever_finished_first(self):
        jobs = [job(i, host="h%d" % i, secs=0.25 - i * 0.05) for i in range(5)]               # the LAST job finishes first
        out = parallel.run_jobs(jobs, workers=5)
        self.assertEqual([o.value for o in out], [0, 1, 2, 3, 4])
        self.assertTrue(all(o.ok for o in out))

    def test_it_really_waits_side_by_side(self):
        jobs = [job(i, host="h%d" % i, secs=0.3) for i in range(6)]
        t = time.monotonic()
        parallel.run_jobs(jobs, workers=6)
        par = time.monotonic() - t
        t = time.monotonic()
        parallel.run_jobs(jobs, workers=1)
        seq = time.monotonic() - t
        self.assertLess(par, 0.9)
        self.assertGreater(seq, 1.6)
        self.assertGreater(seq / par, 3)

    def test_one_worker_is_exactly_the_old_one_after_another(self):
        log = []
        jobs = [job(i, host="x", secs=0.02, log=log) for i in range(4)]
        out = parallel.run_jobs(jobs, workers=1)
        self.assertEqual([k for _, k, _ in log], [0, 1, 2, 3])
        starts = [t for _, _, t in log]
        self.assertTrue(all(b - a >= 0.02 for a, b in zip(starts, starts[1:])))                 # strictly sequential
        self.assertEqual([o.value for o in out], [0, 1, 2, 3])

    def test_same_answers_with_one_worker_or_many(self):
        jobs = [job(i, host="h%d" % (i % 3), secs=0.01, value=i * i) for i in range(12)]
        a = [o.value for o in parallel.run_jobs(jobs, workers=1)]
        b = [o.value for o in parallel.run_jobs(jobs, workers=6)]
        self.assertEqual(a, b)


class TestPoliteness(unittest.TestCase):
    def test_no_more_than_per_host_at_once_on_one_server(self):
        live, peak, lock = [0], [0], threading.Lock()

        def fn():
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.05)
            with lock:
                live[0] -= 1
        jobs = [{"key": i, "host": "same", "fn": fn} for i in range(10)]
        parallel.run_jobs(jobs, workers=8, per_host=2)
        self.assertEqual(peak[0], 2)

    def test_one_busy_site_cannot_starve_the_others(self):
        log = []
        jobs = [job("g%d" % i, host="guardian", secs=0.15, log=log) for i in range(6)] + [job("bbc", host="bbc", secs=0.01, log=log), job("npr", host="npr", secs=0.01, log=log)]
        parallel.run_jobs(jobs, workers=3, per_host=1)
        order = [k for _, k, _ in sorted(log, key=lambda x: x[2])]
        self.assertLess(order.index("bbc"), 4)                                                    # started among the first, not after all six Guardian pages
        self.assertLess(order.index("npr"), 4)

    def test_interleaving_is_round_robin(self):
        self.assertEqual(parallel.interleave(["a", "a", "a", "b", "c", "b"]), [0, 3, 4, 1, 5, 2])

    def test_a_minimum_gap_between_requests_to_one_server(self):
        log = []
        jobs = [job(i, host="gdelt", secs=0.0, log=log) for i in range(3)]
        parallel.run_jobs(jobs, workers=4, min_gap={"gdelt": 0.2})
        starts = sorted(t for _, _, t in log)
        self.assertGreaterEqual(starts[1] - starts[0], 0.18)
        self.assertGreaterEqual(starts[2] - starts[1], 0.18)

    def test_the_sequential_path_sleeps_the_gap_between_same_server_jobs_only(self):
        slept = []
        jobs = [job("g1", host="gdelt"), job("g2", host="gdelt"), job("x", host="other"), job("g3", host="gdelt")]
        parallel.run_jobs(jobs, workers=1, min_gap={"gdelt": 5.5}, sleep=lambda s: slept.append(s))
        self.assertEqual(slept, [5.5, 5.5])                                                       # before g2 and g3, never before the first, never for another server


class TestIsolationAndDeadline(unittest.TestCase):
    def test_one_failure_is_data_not_an_exception_and_does_not_touch_the_others(self):
        jobs = [job(0, host="a"), job(1, host="b", boom=True), job(2, host="c")]
        for workers in (1, 4):
            out = parallel.run_jobs(jobs, workers=workers)
            self.assertEqual([o.ok for o in out], [True, False, True])
            self.assertIsInstance(out[1].error, ValueError)
            self.assertEqual((out[0].value, out[2].value), (0, 2))

    def test_the_deadline_cuts_off_slow_jobs_and_returns_promptly(self):
        jobs = [job("fast", host="a", secs=0.01)] + [job("slow%d" % i, host="s%d" % i, secs=2.0) for i in range(3)]
        t = time.monotonic()
        out = parallel.run_jobs(jobs, workers=4, deadline_s=0.3)
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertTrue(out[0].ok)
        self.assertTrue(all(o.timed_out and not o.ok for o in out[1:]))
        self.assertIsInstance(out[1].error, TimeoutError)

    def test_the_sequential_deadline_stops_starting_new_jobs(self):
        ticks = iter([0, 0, 5, 5, 5, 5])
        jobs = [job(i, host="h%d" % i) for i in range(3)]
        out = parallel.run_jobs(jobs, workers=1, deadline_s=1, clock=lambda: next(ticks))
        self.assertTrue(out[0].ok)
        self.assertTrue(out[1].timed_out and out[2].timed_out)

    def test_seconds_are_measured_per_job(self):
        out = parallel.run_jobs([job("a", host="a", secs=0.1), job("b", host="b", secs=0.0)], workers=2)
        self.assertGreaterEqual(out[0].seconds, 0.09)
        self.assertLess(out[1].seconds, 0.09)

    def test_empty_and_single_job_lists_work(self):
        self.assertEqual(parallel.run_jobs([], workers=6), [])
        self.assertEqual(parallel.run_jobs([job("x")], workers=6)[0].value, "x")


if __name__ == "__main__":
    unittest.main()
