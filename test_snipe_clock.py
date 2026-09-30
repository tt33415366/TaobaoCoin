#!/usr/bin/env python3
"""snipe_clock.py 的测试。运行: python3 test_snipe_clock.py"""

import unittest
from datetime import datetime, timedelta

from snipe_clock import LEAD_SECONDS, SnipeClock, parse_hms


def fixed_clock(dt):
    """返回一个 now_fn：每次调用返回固定时刻。"""
    return lambda: dt


class FakeSleeper:
    def __init__(self):
        self.slept = []

    def __call__(self, seconds):
        self.slept.append(seconds)


class TestParseHms(unittest.TestCase):
    def test_hour_minute(self):
        self.assertEqual(parse_hms("09:59"), (9, 59, 0))

    def test_with_seconds(self):
        self.assertEqual(parse_hms("09:59:55"), (9, 59, 55))

    def test_bad_format_raises(self):
        with self.assertRaises(ValueError):
            parse_hms("abc")


class TestNextRun(unittest.TestCase):
    def setUp(self):
        self.clock = SnipeClock(now_fn=fixed_clock(datetime(2026, 9, 27, 9, 30)),
                                sleep_fn=FakeSleeper(), offset_fn=lambda: 0.0)

    def test_next_run_today(self):
        nxt = self.clock.next_run("10:00")
        self.assertEqual(nxt, datetime(2026, 9, 27, 10, 0))

    def test_next_run_tomorrow_if_passed(self):
        nxt = self.clock.next_run("09:00")
        self.assertEqual(nxt, datetime(2026, 9, 28, 9, 0))

    def test_next_run_tomorrow_if_exact(self):
        clock = SnipeClock(now_fn=fixed_clock(datetime(2026, 9, 27, 10, 0)),
                           sleep_fn=FakeSleeper(), offset_fn=lambda: 0.0)
        self.assertEqual(clock.next_run("10:00"), datetime(2026, 9, 28, 10, 0))

    def test_next_run_with_seconds(self):
        nxt = self.clock.next_run("09:59:55")
        self.assertEqual(nxt, datetime(2026, 9, 27, 9, 59, 55))


class TestWaitFor(unittest.TestCase):
    def test_sleeps_until_snipe_minus_offset_minus_lead(self):
        # 现在 09:59:55，狙击 10:00:00，服务器快 0.1s，提前量 LEAD
        # 期望 sleep = 5s - 0.1s - LEAD
        now = datetime(2026, 9, 27, 9, 59, 55)
        sleeper = FakeSleeper()
        clock = SnipeClock(now_fn=fixed_clock(now), sleep_fn=sleeper,
                           offset_fn=lambda: 0.1)
        clock.wait_for("10:00:00")
        self.assertEqual(len(sleeper.slept), 1)
        self.assertAlmostEqual(sleeper.slept[0], 5 - 0.1 - LEAD_SECONDS, places=3)

    def test_no_sleep_when_snipe_already_passed(self):
        now = datetime(2026, 9, 27, 10, 5)
        sleeper = FakeSleeper()
        clock = SnipeClock(now_fn=fixed_clock(now), sleep_fn=sleeper,
                           offset_fn=lambda: 0.0)
        clock.wait_for("10:00:00")
        self.assertEqual(sleeper.slept, [])

    def test_no_sleep_when_too_far_away(self):
        # 距狙击时刻超过 10 分钟窗口：不睡（避免配置错误导致挂死）
        now = datetime(2026, 9, 27, 8, 0)
        sleeper = FakeSleeper()
        clock = SnipeClock(now_fn=fixed_clock(now), sleep_fn=sleeper,
                           offset_fn=lambda: 0.0)
        clock.wait_for("10:00:00")
        self.assertEqual(sleeper.slept, [])

    def test_negative_offset_shifts_sleep_later(self):
        # 服务器比本地慢 1s：本地要多等 1s 才到服务器整点
        now = datetime(2026, 9, 27, 9, 59, 55)
        sleeper = FakeSleeper()
        clock = SnipeClock(now_fn=fixed_clock(now), sleep_fn=sleeper,
                           offset_fn=lambda: -1.0)
        clock.wait_for("10:00:00")
        self.assertAlmostEqual(sleeper.slept[0], 5 + 1.0 - LEAD_SECONDS, places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
