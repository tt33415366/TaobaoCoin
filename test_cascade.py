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
from gateway import (
    Benefit,
    ExchangeFailed,
    RiskControlBlocked,
    SessionExpired,
    WeeklyLimitReached,
)

TIERS = [
    {"label": "20元红包", "keywords": ["20元", "6000"]},
    {"label": "10元红包", "keywords": ["10元", "3000"]},
    {"label": "5元红包", "keywords": ["5元", "1500"]},
]


def benefit(code, title, coins, amount="", unit=""):
    """按 gateway 的拼法造 match_text（标题 面额单位 金币）。"""
    return Benefit(code=code, title=title, coin_amount=coins,
                   match_text="{} {}{} {}".format(title, amount, unit, coins))


BENEFITS = [
    benefit("code5", "5元红包", 1500),
    benefit("code20", "20元红包", 6000),
    benefit("code10", "10元红包", 3000),
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
        b = benefit("x", "大额红包", 6000)
        tier = {"label": "20元红包", "keywords": ["20元", "6000"]}
        self.assertTrue(benefit_matches_tier(b, tier))


# 真实接口返回的 10元权益（2026-09-30 抓取）解析后的领域对象。
# 当年根因是图床 URL（含 "6000000002272"）漏进匹配面误命中 20元档；
# 如今 URL 过不了 gateway 的 match_text，该回归守卫在 test_gateway。
REALISTIC_10YUAN = benefit("d5af47cdbfe64de9a0e5f39bdd87b0ad", "10元红包", 3000,
                           amount="10", unit="元")


class TestRealisticPayloadMatching(unittest.TestCase):
    """回归：2026-09-30 日志误报「✅ 兑换成功【20元红包】」，实际到账 10元。
    此处守「成功日志以服务端实际面额为准」；URL 误命中根因的守卫已随
    match_text 移至 test_gateway.TestMatchText。"""

    def test_success_label_reflects_actual_award(self):
        # 列表里只剩 10元一项（20元整点售罄被撤下），20元档优先也必须如实报 10元
        fn, calls = stub_exchange({REALISTIC_10YUAN.code: {"displayAmount": "10"}})
        outcome = cascade_exchange([REALISTIC_10YUAN], TIERS, fn)
        self.assertTrue(outcome.success)
        self.assertEqual(calls, [REALISTIC_10YUAN.code])
        self.assertEqual(outcome.tier_label, "10元红包")


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

    def test_fixed_strategy_preserves_config_order(self):
        # fixed = 严格按 tiers 数组顺序，20元 永远第一
        for seed in range(50):
            rng = random.Random(seed)
            order = daily_tier_order(TIERS, rng, strategy="fixed")
            self.assertEqual([t["label"] for t in order],
                             ["20元红包", "10元红包", "5元红包"])

    def test_random_is_default_strategy(self):
        # 不显式传 strategy 时维持随机（向后兼容）
        seen = set()
        rng = random.Random()
        for seed in range(100):
            rng.seed(seed)
            seen.add(daily_tier_order(TIERS, rng)[0]["label"])
        self.assertEqual(len(seen), 2)

    def test_unknown_strategy_raises(self):
        with self.assertRaises(ValueError):
            daily_tier_order(TIERS, random.Random(0), strategy="bogus")


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
        benefits = [benefit("y", "无关权益", 0)]
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
            benefit(None, "20元红包", 0),  # 缺 benefitCode
            benefit("code10", "10元红包", 3000),
        ]
        fn, calls = stub_exchange({"code10": {"award": "10元"}})
        outcome = cascade_exchange(benefits, TIERS, fn)
        self.assertTrue(outcome.success)
        self.assertEqual(calls, ["code10"])


class TestWeeklyLimitOutcome(unittest.TestCase):
    """「已达周限」单独计数：全档周限供状态机判定收菜日，级联语义本身不变。"""

    def test_all_tiers_weekly_limited_flagged(self):
        fn, calls = stub_exchange({
            "code20": WeeklyLimitReached("已达周限"),
            "code10": WeeklyLimitReached("已达周限"),
            "code5": WeeklyLimitReached("已达周限"),
        })
        outcome = cascade_exchange(BENEFITS, TIERS, fn)
        self.assertFalse(outcome.success)
        self.assertTrue(outcome.all_week_limited)
        self.assertEqual(len(calls), 3)

    def test_mixed_weekly_and_sold_out_not_flagged(self):
        # 只要有一档不是周限（如售罄），当天就不是收菜转换信号
        fn, _ = stub_exchange({
            "code20": WeeklyLimitReached("已达周限"),
            "code10": ExchangeFailed("权益已变更，请刷新后重试"),
            "code5": ExchangeFailed("权益已变更，请刷新后重试"),
        })
        outcome = cascade_exchange(BENEFITS, TIERS, fn)
        self.assertFalse(outcome.all_week_limited)

    def test_weekly_limit_still_falls_to_next_tier(self):
        # WeeklyLimitReached 是 ExchangeFailed 子类：级联照常落下一档
        fn, calls = stub_exchange({
            "code20": WeeklyLimitReached("已达周限"),
            "code10": {"award": "10元"},
        })
        outcome = cascade_exchange(BENEFITS, TIERS, fn)
        self.assertTrue(outcome.success)
        self.assertEqual(calls, ["code20", "code10"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
