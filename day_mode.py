#!/usr/bin/env python3
"""当日模式 module（CONTEXT.md：当日模式 DayMode）。

拥有完整日常流程：读探测推导当日模式（狙击日/收菜日/纯收取日）→ 分派执行 →
按「兑换永远优先」收尾收取（ADR 0001 决策 2）。无跨日状态文件：模式每天由当天
首页数据重新推导（决策 4）。

interface 两个动词：
    run_day(cfg, ...)      完整日常流程；features 开关的唯一决策点
                           （--now/--collect 手动路径不读 features）
    run_exchange(cfg, ...) 狙击日的兑换流程（狙击等待→级联→重试），供 --now
                           与 run_day 的狙击日分支复用；不收取、不读 features
另有两个共享函数：collect_and_summarize（常驻与 --collect 的收取+摘要共用，
含 🔑 登录态标记）；log_collection_summary（只做摘要渲染）。retry_interval
是重试间隔纯函数，由 test_day_mode 直接钉住序列。

两次 fetch_benefits 各有其职（ADR 0002）：「模式探测」在 run_time 准点、
只读；「尝试快照」在级联每轮（含首轮）重新拉取——探测早于狙击时刻，
中间隔着整点库存刷新，探测快照不复用于兑换。
"""

import logging
import random
import time
from dataclasses import dataclass
from enum import Enum

from cascade import cascade_exchange, daily_tier_order
from collect import DailyCollectionResult, run_daily_collection
from gateway import TaoCoinGateway

log = logging.getLogger("day_mode")

# 收菜日的收取动作在 run_time 基础上加 0–30 分钟随机抖动（单位：分钟，ADR 0001 决策 3）
COLLECTION_JITTER_MINUTES = 30


class Mode(Enum):
    """当日模式的三个具名值（术语见 CONTEXT.md「时机」一节）。"""
    SNIPE_DAY = "狙击日"
    COLLECTION_DAY = "收菜日"
    PURE_COLLECTION_DAY = "纯收取日"


@dataclass
class ExchangeResult:
    """狙击日兑换流程的终态。week_complete：全档周限或首页全已兑，当天转收菜。
    success 与 week_complete 同为 False 即「重试耗尽仍未兑换成功」。"""
    success: bool
    week_complete: bool = False


@dataclass
class DayResult:
    """一天的完整记录。exchange 只在狙击日非 None；collection 在收取执行过时非 None。
    退出码推导（main 的 exit_code()）：仅狙击日兑换失败（success/week_complete
    皆 False）退出码 1，其余皆 0。"""
    mode: Mode
    exchange: ExchangeResult = None
    collection: DailyCollectionResult = None


def retry_interval(attempt, base, cap):
    """第 attempt 次失败后的等待秒数：指数递增（base, base*2, base*4…），cap 封顶。
    默认 base=1, cap=15 → 1, 2, 4, 8, 15, 15…"""
    return min(cap, base * (2 ** (attempt - 1)))


def run_exchange(cfg, gateway=None, clock=None, rng=random, sleep_fn=time.sleep):
    """执行「狙击等待→查询→级联兑换」，全部售罄按配置递增重试。
    返回 ExchangeResult。SessionExpired / RiskControlBlocked 直接抛出（当天中止）。"""
    gateway = gateway or TaoCoinGateway(cfg.cookie)

    if clock is not None and cfg.snipe_time:
        clock.wait_for(cfg.snipe_time)

    ordered = daily_tier_order(cfg.tiers, rng, strategy=cfg.tier_strategy)
    log.info("今日档位优先级（%s）: %s", cfg.tier_strategy,
             " → ".join(t["label"] for t in ordered))

    for attempt in range(1, cfg.exchange_retries + 1):
        snapshot = gateway.fetch_benefits()  # 尝试快照：每轮刷新，含首轮
        log.info("可兑换列表共 %d 项", len(snapshot.benefits))

        if snapshot.exchanged_all:
            log.info("今天红包已全部兑换过，无需操作")
            return ExchangeResult(success=False, week_complete=True)

        outcome = cascade_exchange(snapshot.benefits, ordered, gateway.exchange)

        if outcome.success:
            return ExchangeResult(success=True)
        if outcome.all_week_limited:
            # 所有匹配档位都报「已达周限」：重试无意义，当天直接转收菜
            log.info("所有匹配档位均已达到本周兑换上限，今日转收菜")
            return ExchangeResult(success=False, week_complete=True)
        if not outcome.attempts:
            # 一档都没匹配上：配置问题，重试无意义
            return ExchangeResult(success=False)

        if attempt < cfg.exchange_retries:
            wait = retry_interval(attempt, cfg.exchange_retry_base,
                                  cfg.exchange_retry_max)
            log.info("第 %d/%d 轮未兑换成功，%.0f 秒后重试…",
                     attempt, cfg.exchange_retries, wait)
            sleep_fn(wait)

    log.error("今日兑换未成功（已试 %d 轮）", cfg.exchange_retries)
    return ExchangeResult(success=False)


def run_day(cfg, gateway=None, clock=None, rng=random, sleep_fn=time.sleep):
    """完整日常流程：先读探测推导当日模式，再分派执行。

    - 兑换关闭 → 纯收取日：不做模式探测（无意义请求不发），抖动后只做收取
    - 收取关闭 → 收菜日探测照旧但不收取不抖动；狙击日兑换结束即完
    - 狙击日的收取排在兑换流程正常结束之后（成功/重试耗尽/全档周限都算正常
      结束），时刻天然不规则，不加抖动；SessionExpired / RiskControlBlocked
      从兑换路径抛出时跳过收取，交给常驻循环的外层网
    收取的任何失败只记日志，绝不影响 DayResult 与退出码。"""
    gateway = gateway or TaoCoinGateway(cfg.cookie)

    if not cfg.exchange_enabled:
        log.info("兑换已关闭（features.exchange=false），今日为纯收取日")
        return DayResult(mode=Mode.PURE_COLLECTION_DAY,
                         collection=_collection_day(gateway, rng, sleep_fn))

    # 模式探测：模式判定的唯一依据（read-only，不消耗兑换机会），run_time 准点发生
    snapshot = gateway.fetch_benefits()
    if snapshot.exchanged_all:
        log.info("首页显示当周红包已全部兑换，今日为收菜日")
        if not cfg.collect_enabled:
            log.info("每日收取已关闭（features.collect=false），今日无事")
            return DayResult(mode=Mode.COLLECTION_DAY)
        return DayResult(mode=Mode.COLLECTION_DAY,
                         collection=_collection_day(gateway, rng, sleep_fn))

    exchange = run_exchange(cfg, gateway=gateway, clock=clock, rng=rng,
                            sleep_fn=sleep_fn)
    collection = None
    if cfg.collect_enabled:
        collection = collect_and_summarize(gateway)
    return DayResult(mode=Mode.SNIPE_DAY, exchange=exchange,
                     collection=collection)


def _collection_day(gateway, rng, sleep_fn):
    """收菜日的收取：抖动只加在收取动作上（避免每天同一时刻的请求画像），
    醒来的模式探测仍对准 run_time。"""
    wait_seconds = rng.uniform(0, COLLECTION_JITTER_MINUTES) * 60
    log.info("收菜日收取动作抖动 %.1f 分钟后执行", wait_seconds / 60)
    if wait_seconds:
        sleep_fn(wait_seconds)
    return collect_and_summarize(gateway)


def collect_and_summarize(gateway):
    """执行一轮每日收取并渲染摘要：常驻（run_day 两条分支）与 --collect 共用。"""
    result = run_daily_collection(gateway)
    log_collection_summary(result)
    return result


def log_collection_summary(result):
    """每日收取收尾：摘要行两种模式共用（绑定格式，不重排）；
    唯独登录态失效要在当日摘要里显著标记。常驻与 --collect 共用本函数。"""
    log.info("%s", result.summary())
    if result.session_expired:
        log.error("🔑 每日收取发现登录态失效，请更新 config.json 里的 cookie")
