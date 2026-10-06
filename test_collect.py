#!/usr/bin/env python3
"""collect.py 编排的测试（fake gateway，不触网）。运行: python3 test_collect.py"""

import unittest

from collect import run_daily_collection
from gateway import (
    CoinTownState,
    ExchangeFailed,
    RiskControlBlocked,
    SessionExpired,
)


class FakeCollectGateway:
    """TaoCoinGateway 位置的假 adapter：按方法名脚本化返回值或异常。
    query_coin_town 给列表则按调用次序弹出（模拟收取前后两次查询）。"""

    def __init__(self, script):
        self.script = script  # {方法名: 返回值 / 异常 / 返回值的列表}
        self.calls = []

    def _run(self, name):
        self.calls.append(name)
        result = self.script.get(name, 0)
        if isinstance(result, list):
            result = result.pop(0) if len(result) > 1 else result[0]
        if isinstance(result, Exception):
            raise result
        return result

    def query_coin_town(self):
        return self._run("query_coin_town")

    def collect_sign_reward(self):
        return self._run("collect_sign_reward")

    def sync_sign_status(self):
        return self._run("sync_sign_status")

    def fetch_benefits(self):
        return self._run("fetch_benefits")


def collect(gateway):
    return run_daily_collection(gateway)


class TestDailyCollection(unittest.TestCase):
    def test_not_signed_signs_then_gained_is_balance_delta(self):
        # 未签到：两次 town 查询夹着签到+同步；+X 取余额差，不信接口自报字段
        gw = FakeCollectGateway({
            "query_coin_town": [CoinTownState(100, False), CoinTownState(140, False)],
            "collect_sign_reward": 999,  # 自报值应被余额差覆盖
        })
        result = collect(gw)
        # 未签：town → 首页热身（页面挂载顺序复刻）→ 签到 → 同步 → 收尾 town
        self.assertEqual(gw.calls, ["query_coin_town", "fetch_benefits",
                                    "collect_sign_reward", "sync_sign_status",
                                    "query_coin_town"])
        self.assertEqual(result.coins_gained, 40)
        self.assertEqual(result.balance, 140)
        self.assertFalse(result.session_expired)

    def test_already_signed_skips_sign_actions(self):
        # 今日已签（实测：已签时 collect.reward.pc 返回空 data）：
        # 不发签到请求，记「今日已签到」，+0，余额照报
        gw = FakeCollectGateway({
            "query_coin_town": CoinTownState(154058, True),
        })
        with self.assertLogs("collect", level="INFO") as cm:
            result = collect(gw)
        self.assertEqual(gw.calls, ["query_coin_town", "query_coin_town"])
        self.assertNotIn("collect_sign_reward", gw.calls)
        self.assertTrue(any("今日已签到" in m for m in cm.output))
        self.assertEqual(result.coins_gained, 0)
        self.assertEqual(result.balance, 154058)

    def test_summary_line_format(self):
        gw = FakeCollectGateway({
            "query_coin_town": [CoinTownState(100, False), CoinTownState(115, False)],
        })
        self.assertEqual(collect(gw).summary(), "今日收取 +15，余额 115")

    def test_initial_town_query_failure_falls_back_to_sign_reward(self):
        # 起始 town 查询失败：仍尝试签到，+X 退化为签到接口自报的 reward
        gw = FakeCollectGateway({
            "query_coin_town": [ExchangeFailed("小镇接口变更"), CoinTownState(0, False)],
            "collect_sign_reward": 7,
        })
        result = collect(gw)
        self.assertEqual(gw.calls, ["query_coin_town", "fetch_benefits",
                                    "collect_sign_reward", "sync_sign_status",
                                    "query_coin_town"])
        self.assertEqual(result.coins_gained, 7)
        self.assertFalse(result.session_expired)

    def test_closing_town_query_failure_keeps_sign_reward(self):
        gw = FakeCollectGateway({
            "query_coin_town": [CoinTownState(100, False), ExchangeFailed("小镇接口变更")],
            "collect_sign_reward": 5,
        })
        result = collect(gw)
        self.assertEqual(result.coins_gained, 5)
        self.assertEqual(result.balance, 100)  # 起始查到的余额仍展示
        self.assertFalse(result.session_expired)

    def test_risk_control_blocked_skips_action_and_continues(self):
        gw = FakeCollectGateway({
            "query_coin_town": CoinTownState(0, False),
            "collect_sign_reward": RiskControlBlocked("触发滑块"),
        })
        result = collect(gw)
        self.assertIn("sync_sign_status", gw.calls)  # 风控只跳过该动作
        self.assertFalse(result.session_expired)

    def test_unexpected_error_on_one_action_does_not_stop_run(self):
        gw = FakeCollectGateway({
            "query_coin_town": [CoinTownState(0, False), CoinTownState(3, False)],
            "sync_sign_status": RuntimeError("返回非 JSON"),
        })
        result = collect(gw)
        self.assertEqual(result.coins_gained, 3)

    def test_session_expired_at_initial_town_query_aborts(self):
        # 登录态失效：起始查询即失败，不尝试签到，显著标记、不抛出
        gw = FakeCollectGateway({
            "query_coin_town": SessionExpired("登录失效"),
        })
        result = collect(gw)
        self.assertTrue(result.session_expired)
        self.assertEqual(gw.calls, ["query_coin_town"])

    def test_session_expired_during_sign_flagged_not_raised(self):
        gw = FakeCollectGateway({
            "query_coin_town": CoinTownState(0, False),
            "collect_sign_reward": SessionExpired("登录失效"),
        })
        result = collect(gw)
        self.assertTrue(result.session_expired)
        self.assertNotIn("sync_sign_status", gw.calls)
        # 已确认登录态死亡后不再发收尾查询
        self.assertEqual(gw.calls.count("query_coin_town"), 1)

    def test_session_expired_at_closing_query_also_flagged(self):
        gw = FakeCollectGateway({
            "query_coin_town": [CoinTownState(100, False), SessionExpired("登录失效")],
            "collect_sign_reward": 5,
        })
        result = collect(gw)
        self.assertTrue(result.session_expired)
        self.assertEqual(result.coins_gained, 5)  # 自报 reward 兜底


if __name__ == "__main__":
    unittest.main(verbosity=2)
