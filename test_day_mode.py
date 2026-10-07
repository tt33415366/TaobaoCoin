#!/usr/bin/env python3
"""day_mode.py 当日模式的测试（fake gateway/clock/rng/sleeper，不触网）。
运行: python3 test_day_mode.py

狙击日/收菜日/纯收取日的规格住在这里——模式推导、抖动、features 分支、
「兑换永远优先」的次序（ADR 0001）。Config 直接构造真对象（普通 kwargs 类）。"""

import random
import unittest

from collect import DailyCollectionResult
from config import Config
from day_mode import (
    Mode,
    log_collection_summary,
    retry_interval,
    run_day,
    run_exchange,
)
from gateway import (
    Benefit,
    CoinTownState,
    ExchangeFailed,
    HomeSnapshot,
    RiskControlBlocked,
    SessionExpired,
    SignOutcome,
    WeeklyLimitReached,
)


def make_cfg(**overrides):
    kwargs = dict(
        cookie="cookie2=x",
        run_time="09:59:30",
        snipe_time="10:00:00",
        exchange_retries=3,
        exchange_retry_base=1,
        exchange_retry_max=15,
        tiers=[
            {"label": "20元红包", "keywords": ["20元", "6000"]},
            {"label": "10元红包", "keywords": ["10元", "3000"]},
            {"label": "5元红包", "keywords": ["5元", "1500"]},
        ],
        tier_strategy="random",
        exchange_enabled=True,
        collect_enabled=True,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def snap(benefits=(), exchanged_all=False):
    return HomeSnapshot(
        exchanged_all=exchanged_all,
        benefits=[Benefit(code=c, title=t, coin_amount=n,
                          match_text="{}  {}".format(t, n))
                  for c, t, n in benefits],
    )


B20 = ("code20", "20元红包", 6000)
B10 = ("code10", "10元红包", 3000)
B5 = ("code5", "5元红包", 1500)


class FakeGateway:
    """TaoCoinGateway 位置的假 adapter：脚本化 fetch/exchange/sign_in/query_coin_town。"""

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
        if isinstance(result, list):
            result = result.pop(0) if len(result) > 1 else result[0]
        if isinstance(result, Exception):
            raise result
        return result

    def _collect(self, name, default):
        self.collect_calls.append(name)
        self.events.append("collect:" + name)
        result = self._collect_script.get(name, default)
        if isinstance(result, Exception):
            raise result
        return result

    def sign_in(self):
        return self._collect("sign_in", SignOutcome(skipped=False, reward=0,
                                                    balance_before=0))

    def query_coin_town(self):
        return self._collect("query_coin_town", CoinTownState(balance=0,
                                                              signed=False))


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


def exchange(gw, clock=None, sleeper=None, cfg=None, rng=None):
    kwargs = dict(gateway=gw, clock=clock, sleep_fn=sleeper or FakeSleeper())
    if rng is not None:
        kwargs["rng"] = rng
    return run_exchange(cfg or make_cfg(), **kwargs)


def day(gw, clock=None, sleeper=None, cfg=None, rng=None):
    kwargs = dict(gateway=gw, clock=clock, sleep_fn=sleeper or FakeSleeper())
    if rng is not None:
        kwargs["rng"] = rng
    return run_day(cfg or make_cfg(), **kwargs)


class TestTierStrategyWiring(unittest.TestCase):
    def test_fixed_strategy_always_tries_20_first(self):
        # rng 种子 1 的 shuffle 会把 10元 排到最前；fixed 策略必须无视随机，
        # 始终 20元 优先
        gw = FakeGateway([snap([B20, B10, B5])],
                         {"code20": {"ok": 1}, "code10": {"ok": 1}})
        exchange(gw, cfg=make_cfg(tier_strategy="fixed"), rng=random.Random(1))
        self.assertEqual(gw.exchange_calls, ["code20"])


class TestRetryInterval(unittest.TestCase):
    def test_exponential_growth_then_capped(self):
        seq = [retry_interval(i, base=1, cap=15) for i in range(1, 9)]
        self.assertEqual(seq, [1, 2, 4, 8, 15, 15, 15, 15])

    def test_never_exceeds_cap(self):
        for i in range(1, 30):
            self.assertLessEqual(retry_interval(i, base=1, cap=15), 15)


class TestRunExchange(unittest.TestCase):
    """狙击日的兑换流程：狙击等待→查询→级联，售罄按配置递增重试。"""

    def test_success_first_pass(self):
        gw = FakeGateway([snap([B20, B10, B5])], {"code20": {"ok": 1}})
        result = exchange(gw)
        self.assertTrue(result.success)
        self.assertFalse(result.week_complete)
        self.assertEqual(gw.fetch_count, 1)

    def test_snipe_wait_happens_before_exchange(self):
        clock = FakeClock()
        gw = FakeGateway([snap([B20])], {"code20": {"ok": 1}})
        exchange(gw, clock=clock)
        self.assertEqual(clock.waited_for, ["10:00:00"])

    def test_retry_after_sold_out_then_success(self):
        # 第一轮全售罄，第二轮 20元有货 → 重试循环生效；每轮刷新尝试快照
        gw = FakeGateway([snap([B20]), snap([B20])],
                         {"code20": [ExchangeFailed("权益已变更"), {"ok": 1}]})
        sleeper = FakeSleeper()
        result = exchange(gw, sleeper=sleeper)
        self.assertTrue(result.success)
        self.assertEqual(gw.fetch_count, 2)
        self.assertEqual(sleeper.slept, [1])  # 第一次失败后等 base=1s

    def test_all_attempts_sold_out_fails(self):
        gw = FakeGateway([snap([B20])], {})
        sleeper = FakeSleeper()
        result = exchange(gw, sleeper=sleeper)
        self.assertFalse(result.success)
        self.assertFalse(result.week_complete)
        self.assertEqual(gw.fetch_count, 3)  # exchange_retries=3
        self.assertEqual(sleeper.slept, [1, 2])  # 1s→2s

    def test_exchanged_all_short_circuits(self):
        gw = FakeGateway([snap(exchanged_all=True)], {})
        result = exchange(gw)
        self.assertTrue(result.week_complete)
        self.assertEqual(gw.exchange_calls, [])

    def test_no_match_fails_without_retry(self):
        # 档位关键词匹配不上是配置问题，重试无意义
        gw = FakeGateway([snap([("codex", "无关权益", 1)])], {})
        sleeper = FakeSleeper()
        result = exchange(gw, sleeper=sleeper)
        self.assertFalse(result.success)
        self.assertEqual(gw.fetch_count, 1)
        self.assertEqual(sleeper.slept, [])

    def test_session_expired_propagates(self):
        gw = FakeGateway([snap([B20])], {"code20": SessionExpired("登录失效")})
        with self.assertRaises(SessionExpired):
            exchange(gw)

    def test_all_tiers_weekly_limited_is_week_complete_without_retry(self):
        # 三档全报「已达周限」：当天立即结束重试，转为收菜
        gw = FakeGateway([snap([B20, B10, B5])], {
            "code20": WeeklyLimitReached("已达周限"),
            "code10": WeeklyLimitReached("已达周限"),
            "code5": WeeklyLimitReached("已达周限"),
        })
        result = exchange(gw)
        self.assertTrue(result.week_complete)
        self.assertFalse(result.success)
        self.assertEqual(gw.fetch_count, 1)


class TestRunDay(unittest.TestCase):
    """当日模式：读探测推导模式，分派执行；features 的唯一决策点（ADR 0001）。"""

    def test_collection_day_probes_then_jitters_then_collects(self):
        # 收菜日：run_time 准点只做读探测；判定收菜后先抖 0–30 分钟再收取
        events = []
        rng = FakeRng(uniform_value=10.0)  # uniform 返回 10 分钟 → 睡 600 秒
        gw = FakeGateway([snap(exchanged_all=True)], {}, events=events)
        sleeper = FakeSleeper(events=events)
        result = day(gw, rng=rng, sleeper=sleeper)
        self.assertEqual(result.mode, Mode.COLLECTION_DAY)
        self.assertIsNone(result.exchange)
        self.assertEqual(gw.exchange_calls, [])  # 收菜日不执行狙击
        self.assertEqual(rng.uniform_calls, [(0, 30)])
        self.assertEqual(sleeper.slept, [600.0])
        # 探测在抖动之前，抖在收取之前
        self.assertEqual(events[0], "fetch")
        self.assertLess(events.index("sleep:600.0"),
                        events.index("collect:sign_in"))
        self.assertIsNotNone(result.collection)

    def test_collection_day_zero_jitter_skips_sleep(self):
        rng = FakeRng(uniform_value=0.0)
        gw = FakeGateway([snap(exchanged_all=True)], {})
        sleeper = FakeSleeper()
        day(gw, rng=rng, sleeper=sleeper)
        self.assertEqual(sleeper.slept, [])

    def test_snipe_day_runs_collection_immediately_without_jitter(self):
        # 狙击日：兑换流程正常结束后立刻收取，时刻天然不规则，不加抖动
        rng = FakeRng()
        gw = FakeGateway([snap([B20, B10, B5])], {"code20": {"ok": 1}})
        sleeper = FakeSleeper()
        result = day(gw, rng=rng, sleeper=sleeper)
        self.assertEqual(result.mode, Mode.SNIPE_DAY)
        self.assertTrue(result.exchange.success)
        self.assertEqual(gw.exchange_calls, ["code20"])
        self.assertEqual(rng.uniform_calls, [])  # 狙击日不调抖动
        self.assertEqual(sleeper.slept, [])
        self.assertEqual(gw.collect_calls, ["sign_in", "query_coin_town"])

    def test_week_complete_day_converts_and_collects(self):
        # 三档全周限 → 当天转收菜：不重试，但收取照常（不带抖动）
        rng = FakeRng()
        gw = FakeGateway([snap([B20, B10, B5])], {
            "code20": WeeklyLimitReached("已达周限"),
            "code10": WeeklyLimitReached("已达周限"),
            "code5": WeeklyLimitReached("已达周限"),
        })
        sleeper = FakeSleeper()
        result = day(gw, rng=rng, sleeper=sleeper)
        self.assertEqual(result.mode, Mode.SNIPE_DAY)  # 探测时是狙击日，级联才发现周限
        self.assertTrue(result.exchange.week_complete)
        # 全档周限不重试：模式探测 1 次 + 级联 1 轮 1 次（两次 fetch 各有其职，
        # 见 ADR 0002：探测早于狙击时刻，快照不复用）
        self.assertEqual(gw.fetch_count, 2)
        self.assertEqual(len(gw.exchange_calls), 3)
        self.assertIn("sign_in", gw.collect_calls)
        self.assertEqual(sleeper.slept, [])  # 周限转换的收取也不抖动

    def test_home_recovered_returns_to_snipe_day(self):
        # 无跨日状态：模式每天由当天首页数据推导，权益恢复自动回到狙击日
        done_gw = FakeGateway([snap(exchanged_all=True)], {})
        self.assertEqual(day(done_gw, rng=FakeRng()).mode, Mode.COLLECTION_DAY)
        fresh_gw = FakeGateway([snap([B20, B10, B5])], {"code20": {"ok": 1}})
        result = day(fresh_gw, rng=FakeRng())
        self.assertEqual(result.mode, Mode.SNIPE_DAY)
        self.assertEqual(fresh_gw.exchange_calls, ["code20"])

    def test_session_expired_from_exchange_skips_collection(self):
        # 兑换路径登录失效：当天中止（外层网处理），跳过收取
        gw = FakeGateway([snap([B20])], {"code20": SessionExpired("登录失效")})
        with self.assertRaises(SessionExpired):
            day(gw)
        self.assertEqual(gw.collect_calls, [])

    def test_risk_control_aborts_skips_collection(self):
        gw = FakeGateway([snap([B20])], {"code20": RiskControlBlocked("滑块")})
        with self.assertRaises(RiskControlBlocked):
            day(gw)
        self.assertEqual(gw.collect_calls, [])

    def test_collection_session_expired_flagged_and_result_unaffected(self):
        # 收取路径发现登录态失效：摘要里显著标 🔑，但不改变当日结果与退出码语义
        gw = FakeGateway([snap(exchanged_all=True)], {},
                         collect_script={"sign_in": SessionExpired("登录失效")})
        with self.assertLogs("day_mode", level="ERROR") as cm:
            result = day(gw, rng=FakeRng(0.0))
        self.assertEqual(result.mode, Mode.COLLECTION_DAY)
        self.assertTrue(result.collection.session_expired)
        self.assertTrue(any("🔑" in m for m in cm.output))

    def test_exchange_failed_day_still_collects(self):
        # 兑换重试耗尽（全档持续报「权益已变更」）也算正常结束：收取照常
        rng = FakeRng()
        gw = FakeGateway([snap([B20, B10, B5])], {
            "code20": ExchangeFailed("权益已变更"),
            "code10": ExchangeFailed("权益已变更"),
            "code5": ExchangeFailed("权益已变更"),
        })
        sleeper = FakeSleeper()
        result = day(gw, cfg=make_cfg(exchange_retries=2), rng=rng,
                     sleeper=sleeper)
        self.assertEqual(result.mode, Mode.SNIPE_DAY)
        self.assertFalse(result.exchange.success)
        self.assertFalse(result.exchange.week_complete)
        # 重试耗尽：模式探测 1 次 + 级联 2 轮
        self.assertEqual(gw.fetch_count, 3)
        self.assertEqual(gw.collect_calls, ["sign_in", "query_coin_town"])
        self.assertEqual(rng.uniform_calls, [])  # 狙击日的收取不抖动

    def test_collection_failure_does_not_change_snipe_day_result(self):
        # 收取的普通失败只记日志：狙击日兑换结果仍是 success
        gw = FakeGateway([snap([B20, B10, B5])], {"code20": {"ok": 1}},
                         collect_script={
                             "sign_in": ExchangeFailed("签到+收币: 今日已签到")})
        result = day(gw, rng=FakeRng())
        self.assertTrue(result.exchange.success)


class TestFeaturesToggle(unittest.TestCase):
    """features 开关：run_day 是开关的唯一决策点（--now/--collect 手动路径不读它）。"""

    def test_exchange_disabled_makes_pure_collection_day(self):
        # 关兑换 → 纯收取日：无模式探测、有抖动、只做收取
        rng = FakeRng(uniform_value=10.0)
        gw = FakeGateway([snap([B20, B10, B5])], {})
        sleeper = FakeSleeper()
        result = day(gw, cfg=make_cfg(exchange_enabled=False), rng=rng,
                     sleeper=sleeper)
        self.assertEqual(result.mode, Mode.PURE_COLLECTION_DAY)
        self.assertIsNone(result.exchange)
        self.assertEqual(gw.fetch_count, 0)  # 无模式探测
        self.assertEqual(gw.exchange_calls, [])
        self.assertEqual(rng.uniform_calls, [(0, 30)])
        self.assertEqual(sleeper.slept, [600.0])
        self.assertIn("sign_in", gw.collect_calls)

    def test_collect_disabled_snipe_day_skips_collection(self):
        # 关收取的狙击日：兑换照常，结束即完，无收取
        gw = FakeGateway([snap([B20, B10, B5])], {"code20": {"ok": 1}})
        result = day(gw, cfg=make_cfg(collect_enabled=False), rng=FakeRng())
        self.assertEqual(result.mode, Mode.SNIPE_DAY)
        self.assertEqual(gw.exchange_calls, ["code20"])
        self.assertEqual(gw.collect_calls, [])
        self.assertIsNone(result.collection)

    def test_collect_disabled_collection_day_is_quiet(self):
        # 关收取的收菜日：探测仍做（模式判定），但不抖动、不收取、不空转
        rng = FakeRng()
        gw = FakeGateway([snap(exchanged_all=True)], {})
        sleeper = FakeSleeper()
        result = day(gw, cfg=make_cfg(collect_enabled=False), rng=rng,
                     sleeper=sleeper)
        self.assertEqual(result.mode, Mode.COLLECTION_DAY)
        self.assertEqual(gw.fetch_count, 1)
        self.assertEqual(gw.collect_calls, [])
        self.assertEqual(rng.uniform_calls, [])
        self.assertEqual(sleeper.slept, [])
        self.assertIsNone(result.collection)


class TestLogCollectionSummary(unittest.TestCase):
    """收取摘要渲染：常驻（run_day 内部）与 --collect 手动路径共用这一个函数。"""

    def test_session_expired_marks_key_emoji(self):
        with self.assertLogs("day_mode", level="ERROR") as cm:
            log_collection_summary(DailyCollectionResult(coins_gained=0,
                                                         balance=0,
                                                         session_expired=True))
        self.assertTrue(any("🔑" in m for m in cm.output))

    def test_normal_summary_without_key_emoji(self):
        with self.assertLogs("day_mode", level="INFO") as cm:
            log_collection_summary(DailyCollectionResult(coins_gained=5,
                                                         balance=100))
        self.assertTrue(any("今日收取 +5，余额 100" in m for m in cm.output))
        self.assertFalse(any("🔑" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
