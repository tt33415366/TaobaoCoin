#!/usr/bin/env python3
"""taobao_coin.py 入口编排的测试。运行: python3 test_main.py"""

import random
import unittest

from gateway import (
    Benefit,
    CoinTownState,
    ExchangeFailed,
    HomeSnapshot,
    RiskControlBlocked,
    SessionExpired,
    WeeklyLimitReached,
)
from taobao_coin import DailyRunResult, retry_interval, run_daily_flow, run_once


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
    """TaoCoinGateway 位置的假 adapter：脚本化每轮 fetch/exchange/collect 的结果。"""

    def __init__(self, snapshots, exchange_script, collect_script=None, events=None):
        self._snapshots = list(snapshots)
        self._exchange_script = exchange_script
        self._collect_script = collect_script or {}
        self.events = events if events is not None else []
        self.fetch_count = 0
        self.exchange_calls = []
        self.collect_calls = []

    def fetch_benefits(self):
        self.fetch_count += 1
        self.events.append("fetch")
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]

    def exchange(self, code):
        self.exchange_calls.append(code)
        self.events.append("exchange:" + code)
        result = self._exchange_script.get(code, ExchangeFailed("售罄"))
        if isinstance(result, Exception):
            raise result
        return result

    def _collect(self, name):
        self.collect_calls.append(name)
        self.events.append("collect:" + name)
        result = self._collect_script.get(name, 0)
        if isinstance(result, Exception):
            raise result
        return result

    def collect_sign_reward(self):
        return self._collect("collect_sign_reward")

    def sync_sign_status(self):
        return self._collect("sync_sign_status")

    def query_coin_town(self):
        # 小镇状态（余额+是否已签）：collect_script 可覆盖，默认未签、余额 0
        self.collect_calls.append("query_coin_town")
        self.events.append("collect:query_coin_town")
        result = self._collect_script.get("query_coin_town",
                                          CoinTownState(balance=0, signed=False))
        if isinstance(result, Exception):
            raise result
        return result


class FakeClock:
    def __init__(self):
        self.waited_for = []

    def wait_for(self, snipe_time):
        self.waited_for.append(snipe_time)


class FakeSleeper:
    def __init__(self, events=None):
        self.slept = []
        self.events = events

    def __call__(self, s):
        self.slept.append(s)
        if self.events is not None:
            self.events.append("sleep:{}".format(s))


class FakeRng:
    """random 模块位置的假：脚本化 uniform（收菜日抖动用），shuffle 保持原序。"""

    def __init__(self, uniform_value=600.0):
        self.uniform_value = uniform_value
        self.uniform_calls = []

    def shuffle(self, xs):
        pass

    def uniform(self, a, b):
        self.uniform_calls.append((a, b))
        return self.uniform_value


class FakeCfg:
    cookie = "cookie2=x"
    snipe_time = "10:00:00"
    exchange_retries = 3
    exchange_retry_base = 1
    exchange_retry_max = 15
    tier_strategy = "random"
    tiers = [
        {"label": "20元红包", "keywords": ["20元", "6000"]},
        {"label": "10元红包", "keywords": ["10元", "3000"]},
        {"label": "5元红包", "keywords": ["5元", "1500"]},
    ]


def run(gw, clock=None, sleeper=None, cfg=None, rng=None):
    kwargs = dict(gateway=gw, clock=clock, sleep_fn=sleeper or FakeSleeper())
    if rng is not None:
        kwargs["rng"] = rng
    return run_once(cfg or FakeCfg(), **kwargs)


def run_flow(gw, clock=None, sleeper=None, cfg=None, rng=None):
    kwargs = dict(gateway=gw, clock=clock, sleep_fn=sleeper or FakeSleeper())
    if rng is not None:
        kwargs["rng"] = rng
    return run_daily_flow(cfg or FakeCfg(), **kwargs)


class TestTierStrategyWiring(unittest.TestCase):
    def test_fixed_strategy_always_tries_20_first(self):
        # rng 种子 1 的 shuffle 会把 10元 排到最前；fixed 策略必须无视随机，
        # 始终 20元 优先
        cfg = FakeCfg()
        cfg.tier_strategy = "fixed"
        gw = FakeGateway([snap([B20, B10, B5])],
                         {"code20": {"ok": 1}, "code10": {"ok": 1}})
        run(gw, cfg=cfg, rng=random.Random(1))
        self.assertEqual(gw.exchange_calls, ["code20"])


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

    def test_all_tiers_weekly_limited_returns_week_complete_without_retry(self):
        # 三档全报「已达周限」：当天立即结束重试，转为收菜日（值仍 truthy）
        gw = FakeGateway([snap([B20, B10, B5])], {
            "code20": WeeklyLimitReached("已达周限"),
            "code10": WeeklyLimitReached("已达周限"),
            "code5": WeeklyLimitReached("已达周限"),
        })
        self.assertEqual(run(gw), DailyRunResult.WEEK_COMPLETE)
        self.assertEqual(gw.fetch_count, 1)


class TestRunDailyFlow(unittest.TestCase):
    def test_collection_day_probes_then_jitters_then_collects(self):
        # 收菜日：run_time 准点只做读探测；判定收菜后先抖 0–30 分钟再收取
        events = []
        rng = FakeRng(uniform_value=10.0)  # uniform 返回 10 分钟 → 睡 600 秒
        gw = FakeGateway([snap(exchanged_all=True)], {},
                         collect_script={"collect_sign_reward": 5},
                         events=events)
        sleeper = FakeSleeper(events=events)
        result = run_flow(gw, rng=rng, sleeper=sleeper)
        self.assertEqual(result, DailyRunResult.WEEK_COMPLETE)
        self.assertEqual(gw.exchange_calls, [])  # 收菜日不执行狙击
        self.assertEqual(rng.uniform_calls, [(0, 30)])
        self.assertEqual(sleeper.slept, [600.0])
        # 探测在抖动之前，抖在收取之前
        self.assertEqual(events[0], "fetch")
        self.assertLess(events.index("sleep:600.0"),
                        events.index("collect:collect_sign_reward"))

    def test_collection_day_zero_jitter_skips_sleep(self):
        rng = FakeRng(uniform_value=0.0)
        gw = FakeGateway([snap(exchanged_all=True)], {})
        sleeper = FakeSleeper()
        run_flow(gw, rng=rng, sleeper=sleeper)
        self.assertEqual(sleeper.slept, [])

    def test_snipe_day_runs_collection_immediately_without_jitter(self):
        # 狙击日：兑换流程正常结束后立刻收取，时刻天然不规则，不加抖动
        rng = FakeRng()
        gw = FakeGateway([snap([B20, B10, B5])], {"code20": {"ok": 1}},
                         collect_script={"collect_sign_reward": 3})
        sleeper = FakeSleeper()
        result = run_flow(gw, rng=rng, sleeper=sleeper)
        self.assertEqual(result, DailyRunResult.SUCCESS)
        self.assertEqual(gw.exchange_calls, ["code20"])
        self.assertEqual(rng.uniform_calls, [])  # 狙击日不调抖动
        self.assertEqual(sleeper.slept, [])
        self.assertEqual(gw.collect_calls, ["query_coin_town",
                                            "collect_sign_reward",
                                            "sync_sign_status",
                                            "query_coin_town"])

    def test_week_complete_day_converts_and_collects(self):
        # 三档全周限 → 当天转收菜：不重试，但收取照常（不带抖动）
        rng = FakeRng()
        gw = FakeGateway([snap([B20, B10, B5])], {
            "code20": WeeklyLimitReached("已达周限"),
            "code10": WeeklyLimitReached("已达周限"),
            "code5": WeeklyLimitReached("已达周限"),
        }, collect_script={"collect_sign_reward": 4})
        sleeper = FakeSleeper()
        result = run_flow(gw, rng=rng, sleeper=sleeper)
        self.assertEqual(result, DailyRunResult.WEEK_COMPLETE)
        # 全档周限不重试：探测 1 次 + 级联 1 轮 1 次（收尾余额走 town，不算 fetch）
        self.assertEqual(gw.fetch_count, 2)
        self.assertEqual(len(gw.exchange_calls), 3)
        self.assertIn("collect_sign_reward", gw.collect_calls)
        self.assertEqual(sleeper.slept, [])  # 周限转换的收取也不抖动

    def test_home_recovered_returns_to_snipe_day(self):
        # 无跨日状态：模式每天由当天首页数据判定，权益恢复自动回到狙击日
        done_gw = FakeGateway([snap(exchanged_all=True)], {})
        self.assertEqual(run_flow(done_gw, rng=FakeRng()),
                         DailyRunResult.WEEK_COMPLETE)
        fresh_gw = FakeGateway([snap([B20, B10, B5])], {"code20": {"ok": 1}},
                               collect_script={"collect_sign_reward": 1})
        self.assertEqual(run_flow(fresh_gw, rng=FakeRng()),
                         DailyRunResult.SUCCESS)
        self.assertEqual(fresh_gw.exchange_calls, ["code20"])

    def test_session_expired_from_exchange_skips_collection(self):
        # 兑换路径登录失效：当天中止（外层网处理），跳过收取
        gw = FakeGateway([snap([B20])], {"code20": SessionExpired("登录失效")})
        with self.assertRaises(SessionExpired):
            run_flow(gw)
        self.assertEqual(gw.collect_calls, [])

    def test_risk_control_aborts_skips_collection(self):
        gw = FakeGateway([snap([B20])], {"code20": RiskControlBlocked("滑块")})
        with self.assertRaises(RiskControlBlocked):
            run_flow(gw)
        self.assertEqual(gw.collect_calls, [])

    def test_collection_session_expired_flagged_and_result_unaffected(self):
        # 收取路径发现登录态失效：摘要里显著标 🔑，但不改变当日结果与退出码语义
        gw = FakeGateway([snap(exchanged_all=True)], {},
                         collect_script={
                             "collect_sign_reward": SessionExpired("登录失效")})
        with self.assertLogs("taobao_coin", level="ERROR") as cm:
            result = run_flow(gw, rng=FakeRng(0.0))
        self.assertEqual(result, DailyRunResult.WEEK_COMPLETE)
        self.assertTrue(any("🔑" in m for m in cm.output))

    def test_exchange_failed_day_still_collects(self):
        # 兑换重试耗尽（全档持续报「权益已变更」）也算正常结束：收取照常，结果 FAILED
        cfg = FakeCfg()
        cfg.exchange_retries = 2
        rng = FakeRng()
        gw = FakeGateway([snap([B20, B10, B5])], {
            "code20": ExchangeFailed("权益已变更"),
            "code10": ExchangeFailed("权益已变更"),
            "code5": ExchangeFailed("权益已变更"),
        }, collect_script={"collect_sign_reward": 2})
        sleeper = FakeSleeper()
        result = run_flow(gw, cfg=cfg, rng=rng, sleeper=sleeper)
        self.assertEqual(result, DailyRunResult.FAILED)
        # 重试耗尽：探测 1 次 + 级联 2 轮（收尾余额走 town，不算 fetch）
        self.assertEqual(gw.fetch_count, 3)
        self.assertEqual(gw.collect_calls, ["query_coin_town",
                                            "collect_sign_reward",
                                            "sync_sign_status",
                                            "query_coin_town"])
        self.assertEqual(rng.uniform_calls, [])  # 狙击日的收取不抖动

    def test_collection_failure_does_not_change_snipe_day_result(self):
        # 收取的普通失败只记日志：狙击日结果仍是 SUCCESS
        gw = FakeGateway([snap([B20, B10, B5])], {"code20": {"ok": 1}},
                         collect_script={
                             "collect_sign_reward": ExchangeFailed("今日已签到")})
        result = run_flow(gw, rng=FakeRng())
        self.assertEqual(result, DailyRunResult.SUCCESS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
