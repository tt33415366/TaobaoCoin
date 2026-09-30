#!/usr/bin/env python3
"""taobao_coin.py 入口编排的测试。运行: python3 test_main.py"""

import unittest

from gateway import ExchangeFailed, HomeSnapshot, Benefit, SessionExpired
from taobao_coin import retry_interval, run_once


def snap(benefits=(), exchanged_all=False):
    return HomeSnapshot(
        exchanged_all=exchanged_all,
        benefits=[Benefit(code=c, title=t, coin_amount=n, raw={
            "benefitCode": c, "displayTitle": t, "reduceCoinAmount": n})
            for c, t, n in benefits],
    )


B20 = ("code20", "20元红包", 6000)
B10 = ("code10", "10元红包", 3000)
B5 = ("code5", "5元红包", 1500)


class FakeGateway:
    """TaoCoinGateway 位置的假 adapter：脚本化每轮 fetch/exchange 的结果。"""

    def __init__(self, snapshots, exchange_script):
        self._snapshots = list(snapshots)
        self._exchange_script = exchange_script
        self.fetch_count = 0
        self.exchange_calls = []

    def fetch_benefits(self):
        self.fetch_count += 1
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]

    def exchange(self, code):
        self.exchange_calls.append(code)
        result = self._exchange_script.get(code, ExchangeFailed("售罄"))
        if isinstance(result, Exception):
            raise result
        return result


class FakeClock:
    def __init__(self):
        self.waited_for = []

    def wait_for(self, snipe_time):
        self.waited_for.append(snipe_time)


class FakeSleeper:
    def __init__(self):
        self.slept = []

    def __call__(self, s):
        self.slept.append(s)


class FakeCfg:
    cookie = "cookie2=x"
    snipe_time = "10:00:00"
    exchange_retries = 3
    exchange_retry_base = 1
    exchange_retry_max = 15
    tiers = [
        {"label": "20元红包", "keywords": ["20元", "6000"]},
        {"label": "10元红包", "keywords": ["10元", "3000"]},
        {"label": "5元红包", "keywords": ["5元", "1500"]},
    ]


def run(gw, clock=None, sleeper=None, cfg=None):
    return run_once(cfg or FakeCfg(), gateway=gw, clock=clock,
                    sleep_fn=sleeper or FakeSleeper())


class TestRetryInterval(unittest.TestCase):
    def test_exponential_growth_then_capped(self):
        seq = [retry_interval(i, base=1, cap=15) for i in range(1, 9)]
        self.assertEqual(seq, [1, 2, 4, 8, 15, 15, 15, 15])

    def test_never_exceeds_cap(self):
        for i in range(1, 30):
            self.assertLessEqual(retry_interval(i, base=1, cap=15), 15)


class TestRunOnce(unittest.TestCase):
    def test_success_first_pass(self):
        gw = FakeGateway([snap([B20, B10, B5])], {"code20": {"ok": 1}})
        self.assertTrue(run(gw))
        self.assertEqual(gw.fetch_count, 1)

    def test_snipe_wait_happens_before_exchange(self):
        clock = FakeClock()
        gw = FakeGateway([snap([B20])], {"code20": {"ok": 1}})
        run(gw, clock=clock)
        self.assertEqual(clock.waited_for, ["10:00:00"])

    def test_retry_after_sold_out_then_success(self):
        # 第一轮全售罄，第二轮 20元有货 → 重试循环生效
        gw = FakeGateway(
            [snap([B20]), snap([B20])],
            {"code20": [ExchangeFailed("权益已变更"), {"ok": 1}]},
        )
        # exchange_script 需要按调用次数变化：用列表脚本
        class SeqGateway(FakeGateway):
            def exchange(self, code):
                self.exchange_calls.append(code)
                seq = self._exchange_script[code]
                r = seq.pop(0) if len(seq) > 1 else seq[0]
                if isinstance(r, Exception):
                    raise r
                return r
        gw = SeqGateway([snap([B20]), snap([B20])],
                        {"code20": [ExchangeFailed("权益已变更"), {"ok": 1}]})
        sleeper = FakeSleeper()
        self.assertTrue(run(gw, sleeper=sleeper))
        self.assertEqual(gw.fetch_count, 2)
        self.assertEqual(sleeper.slept, [1])  # 第一次失败后等 base=1s

    def test_all_attempts_sold_out_returns_false(self):
        gw = FakeGateway([snap([B20])], {})
        sleeper = FakeSleeper()
        self.assertFalse(run(gw, sleeper=sleeper))
        self.assertEqual(gw.fetch_count, 3)  # exchange_retries=3
        self.assertEqual(sleeper.slept, [1, 2])  # 1s→2s

    def test_exchanged_all_short_circuits(self):
        gw = FakeGateway([snap(exchanged_all=True)], {})
        self.assertTrue(run(gw))
        self.assertEqual(gw.exchange_calls, [])

    def test_no_match_returns_false_without_retry(self):
        # 档位关键词匹配不上是配置问题，重试无意义
        gw = FakeGateway([snap([("codex", "无关权益", 1)])], {})
        sleeper = FakeSleeper()
        self.assertFalse(run(gw, sleeper=sleeper))
        self.assertEqual(gw.fetch_count, 1)
        self.assertEqual(sleeper.slept, [])

    def test_session_expired_propagates(self):
        gw = FakeGateway([snap([B20])], {"code20": SessionExpired("登录失效")})
        with self.assertRaises(SessionExpired):
            run(gw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
