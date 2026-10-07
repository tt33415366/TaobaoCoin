#!/usr/bin/env python3
"""collect.py 编排的测试（fake gateway，不触网）。运行: python3 test_collect.py

fake 只需实现两个动词：sign_in（页面挂载序列在 gateway 内部）与
query_coin_town（收尾余额查询，+X 测量的后半截）。"""

import unittest

from collect import run_daily_collection
from gateway import (
    CoinTownState,
    ExchangeFailed,
    RiskControlBlocked,
    SessionExpired,
    SignOutcome,
)


class FakeCollectGateway:
    """TaoCoinGateway 位置的假 adapter：按方法名脚本化返回值或异常。"""

    def __init__(self, script):
        self.script = script  # {方法名: 返回值 / 异常}
        self.calls = []

    def _run(self, name):
        self.calls.append(name)
        result = self.script.get(name, SignOutcome(skipped=False, reward=0))
        if isinstance(result, Exception):
            raise result
        return result

    def sign_in(self):
        return self._run("sign_in")

    def query_coin_town(self):
        return self._run("query_coin_town")


def collect(gateway):
    return run_daily_collection(gateway)


class TestDailyCollection(unittest.TestCase):
    def test_not_signed_gained_is_balance_delta(self):
        # 未签到：sign_in 后收尾 town 查询；+X 取余额差，不信接口自报字段
        gw = FakeCollectGateway({
            "sign_in": SignOutcome(skipped=False, reward=999, balance_before=100),
            "query_coin_town": CoinTownState(140, False),
        })
        result = collect(gw)
        self.assertEqual(gw.calls, ["sign_in", "query_coin_town"])
        self.assertEqual(result.coins_gained, 40)
        self.assertEqual(result.balance, 140)
        self.assertFalse(result.session_expired)

    def test_already_signed_logs_and_reports_zero(self):
        # 今日已签（gateway 内短路）：记「今日已签到」，+0，余额照报
        gw = FakeCollectGateway({
            "sign_in": SignOutcome(skipped=True, reward=0, balance_before=154058),
            "query_coin_town": CoinTownState(154058, True),
        })
        with self.assertLogs("collect", level="INFO") as cm:
            result = collect(gw)
        self.assertTrue(any("今日已签到" in m for m in cm.output))
        self.assertEqual(result.coins_gained, 0)
        self.assertEqual(result.balance, 154058)

    def test_summary_line_format(self):
        gw = FakeCollectGateway({
            "sign_in": SignOutcome(skipped=False, reward=0, balance_before=100),
            "query_coin_town": CoinTownState(115, False),
        })
        self.assertEqual(collect(gw).summary(), "今日收取 +15，余额 115")

    def test_missing_balance_before_falls_back_to_sign_reward(self):
        # 起始 town 查询失败（balance_before=None）：+X 退化为签到接口自报的 reward
        gw = FakeCollectGateway({
            "sign_in": SignOutcome(skipped=False, reward=7, balance_before=None),
            "query_coin_town": CoinTownState(0, False),
        })
        result = collect(gw)
        self.assertEqual(result.coins_gained, 7)
        self.assertFalse(result.session_expired)

    def test_closing_town_query_failure_keeps_sign_reward(self):
        gw = FakeCollectGateway({
            "sign_in": SignOutcome(skipped=False, reward=5, balance_before=100),
            "query_coin_town": ExchangeFailed("小镇接口变更"),
        })
        result = collect(gw)
        self.assertEqual(result.coins_gained, 5)
        self.assertEqual(result.balance, 100)  # 起始查到的余额仍展示
        self.assertFalse(result.session_expired)

    def test_risk_control_blocked_continues_to_closing_query(self):
        # 风控：跳过签到，但收尾测量照常（登录态未死）
        gw = FakeCollectGateway({
            "sign_in": RiskControlBlocked("触发滑块"),
            "query_coin_town": CoinTownState(3, False),
        })
        result = collect(gw)
        self.assertEqual(gw.calls, ["sign_in", "query_coin_town"])
        self.assertFalse(result.session_expired)

    def test_unexpected_error_does_not_stop_run(self):
        gw = FakeCollectGateway({
            "sign_in": RuntimeError("返回非 JSON"),
            "query_coin_town": CoinTownState(3, False),
        })
        result = collect(gw)
        self.assertEqual(gw.calls, ["sign_in", "query_coin_town"])
        self.assertEqual(result.balance, 3)
        self.assertFalse(result.session_expired)

    def test_business_failure_is_normal_ending(self):
        # 当日已签到等业务失败（ExchangeFailed）：记日志，不 fatal，收尾照常
        gw = FakeCollectGateway({
            "sign_in": ExchangeFailed("签到+收币: 今日已签到"),
            "query_coin_town": CoinTownState(200, True),
        })
        result = collect(gw)
        self.assertEqual(gw.calls, ["sign_in", "query_coin_town"])
        self.assertFalse(result.session_expired)

    def test_session_expired_aborts_and_flags(self):
        # 登录态失效：不抛出、记进 session_expired；收尾查询也不再发
        gw = FakeCollectGateway({
            "sign_in": SessionExpired("登录失效"),
        })
        result = collect(gw)
        self.assertTrue(result.session_expired)
        self.assertEqual(gw.calls, ["sign_in"])

    def test_session_expired_at_closing_query_also_flagged(self):
        gw = FakeCollectGateway({
            "sign_in": SignOutcome(skipped=False, reward=5, balance_before=100),
            "query_coin_town": SessionExpired("登录失效"),
        })
        result = collect(gw)
        self.assertTrue(result.session_expired)
        self.assertEqual(result.coins_gained, 5)  # 自报 reward 兜底


if __name__ == "__main__":
    unittest.main(verbosity=2)
