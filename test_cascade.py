#!/usr/bin/env python3
"""cascade.py 的测试。运行: python3 test_cascade.py"""

import random
import unittest

from cascade import (
    ExchangeOutcome,
    benefit_matches_tier,
    cascade_exchange,
    daily_tier_order,
)
from gateway import ExchangeFailed, RiskControlBlocked, SessionExpired

TIERS = [
    {"label": "20元红包", "keywords": ["20元", "6000"]},
    {"label": "10元红包", "keywords": ["10元", "3000"]},
    {"label": "5元红包", "keywords": ["5元", "1500"]},
]

BENEFITS = [
    {"benefitCode": "code5", "displayTitle": "5元红包", "reduceCoinAmount": 1500},
    {"benefitCode": "code20", "displayTitle": "20元红包", "reduceCoinAmount": 6000},
    {"benefitCode": "code10", "displayTitle": "10元红包", "reduceCoinAmount": 3000},
]


def stub_exchange(script):
    """script: {benefitCode: award_dict 或 异常实例}。记录调用顺序。"""
    calls = []

    def fn(code):
        calls.append(code)
        result = script[code]
        if isinstance(result, Exception):
            raise result
        return result

    return fn, calls


class TestMatching(unittest.TestCase):
    def test_match_by_title(self):
        tier = {"label": "10元红包", "keywords": ["10元", "3000"]}
        self.assertTrue(benefit_matches_tier(BENEFITS[2], tier))
        self.assertFalse(benefit_matches_tier(BENEFITS[0], tier))

    def test_match_by_coin_count_fallback(self):
        benefit = {"benefitCode": "x", "displayTitle": "大额红包", "reduceCoinAmount": 6000}
        tier = {"label": "20元红包", "keywords": ["20元", "6000"]}
        self.assertTrue(benefit_matches_tier(benefit, tier))


class TestDailyTierOrder(unittest.TestCase):
    def test_last_tier_always_last(self):
        rng = random.Random()
        for seed in range(50):
            rng.seed(seed)
            order = daily_tier_order(TIERS, rng)
            self.assertEqual(order[-1]["label"], "5元红包")
            self.assertEqual(sorted(t["label"] for t in order[:2]),
                             ["10元红包", "20元红包"])

    def test_random_actually_varies(self):
        seen = set()
        rng = random.Random()
        for seed in range(100):
            rng.seed(seed)
            seen.add(daily_tier_order(TIERS, rng)[0]["label"])
        self.assertEqual(seen, {"10元红包", "20元红包"})


class TestCascade(unittest.TestCase):
    def test_first_tier_success(self):
        fn, calls = stub_exchange({"code20": {"award": "20元"}})
        outcome = cascade_exchange(BENEFITS, TIERS, fn)
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.tier_label, "20元红包")
        self.assertEqual(outcome.award, {"award": "20元"})
        self.assertEqual(calls, ["code20"])

    def test_sold_out_falls_to_next_tier(self):
        # 20元售罄 → 落到 10元成功
        fn, calls = stub_exchange({
            "code20": ExchangeFailed("权益已变更，请刷新后重试"),
            "code10": {"award": "10元"},
        })
        outcome = cascade_exchange(BENEFITS, TIERS, fn)
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.tier_label, "10元红包")
        self.assertEqual(calls, ["code20", "code10"])
        self.assertEqual(len(outcome.attempts), 2)
        self.assertIn("权益已变更", outcome.attempts[0][1])

    def test_all_tiers_fail_returns_unsuccessful_outcome(self):
        fn, calls = stub_exchange({
            "code20": ExchangeFailed("售罄"),
            "code10": ExchangeFailed("售罄"),
            "code5": ExchangeFailed("售罄"),
        })
        outcome = cascade_exchange(BENEFITS, TIERS, fn)
        self.assertFalse(outcome.success)
        self.assertIsNone(outcome.tier_label)
        self.assertEqual(len(calls), 3)

    def test_risk_control_aborts_immediately(self):
        # 风控不属于级联：立刻中止，不打后面的档位
        fn, calls = stub_exchange({
            "code20": RiskControlBlocked("滑块"),
            "code10": {"award": "10元"},
        })
        with self.assertRaises(RiskControlBlocked):
            cascade_exchange(BENEFITS, TIERS, fn)
        self.assertEqual(calls, ["code20"])

    def test_session_expired_aborts_immediately(self):
        fn, calls = stub_exchange({
            "code20": SessionExpired("登录失效"),
            "code10": {"award": "10元"},
        })
        with self.assertRaises(SessionExpired):
            cascade_exchange(BENEFITS, TIERS, fn)
        self.assertEqual(calls, ["code20"])

    def test_no_matching_benefit(self):
        benefits = [{"benefitCode": "y", "displayTitle": "无关权益"}]
        fn, calls = stub_exchange({})
        outcome = cascade_exchange(benefits, TIERS, fn)
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.attempts, [])
        self.assertEqual(calls, [])

    def test_empty_benefit_list(self):
        fn, _ = stub_exchange({})
        outcome = cascade_exchange([], TIERS, fn)
        self.assertFalse(outcome.success)

    def test_tier_order_respected(self):
        # 传入 10元 优先时，即便 20元 在列表前面也先打 10元
        fn, calls = stub_exchange({"code10": {"award": "10元"}})
        outcome = cascade_exchange(BENEFITS, [TIERS[1], TIERS[0], TIERS[2]], fn)
        self.assertTrue(outcome.success)
        self.assertEqual(calls, ["code10"])

    def test_benefit_without_code_skipped(self):
        benefits = [
            {"displayTitle": "20元红包"},  # 缺 benefitCode
            {"benefitCode": "code10", "displayTitle": "10元红包", "reduceCoinAmount": 3000},
        ]
        fn, calls = stub_exchange({"code10": {"award": "10元"}})
        outcome = cascade_exchange(benefits, TIERS, fn)
        self.assertTrue(outcome.success)
        self.assertEqual(calls, ["code10"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
