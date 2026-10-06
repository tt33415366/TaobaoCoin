#!/usr/bin/env python3
"""
淘宝淘金币 - 每天自动兑换红包（薄入口）

编排各 module：
  config.Config      配置加载与校验
  snipe_clock        狙击时钟（准点对准 10:00 库存刷新）
  gateway            淘金币 Gateway（mtop 协议细节全在里面）
  cascade            档位级联（20元/10元随机优先，5元兜底）

用法：
  python3 taobao_coin.py            # 常驻，每天到 config.json 里的 run_time 执行
  python3 taobao_coin.py --now      # 立刻执行一次（测试用）
  python3 taobao_coin.py --list     # 只拉取并打印当前可兑换列表，不兑换
  python3 taobao_coin.py --collect  # 立刻执行一次每日收取（签到+收币/同步/下单返币）
"""

import logging
import random
import sys
import time
from datetime import datetime
from enum import IntEnum
from pathlib import Path

from cascade import cascade_exchange, daily_tier_order
from collect import run_daily_collection
from config import Config
from gateway import ExchangeFailed, RiskControlBlocked, SessionExpired, TaoCoinGateway
from snipe_clock import SnipeClock

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "taobao_coin.log"

# 收菜日的收取动作在 run_time 基础上加 0–30 分钟随机抖动（单位：分钟）
COLLECTION_JITTER_MINUTES = 30

log = logging.getLogger("taobao_coin")


class DailyRunResult(IntEnum):
    """run_once / run_daily_flow 的终态。IntEnum 保持 bool/退出码语义：
    FAILED 为 0（退出码 1），SUCCESS / WEEK_COMPLETE 为 truthy（退出码 0）。"""
    FAILED = 0          # 重试耗尽仍未兑换成功
    SUCCESS = 1         # 兑换成功
    WEEK_COMPLETE = 2   # 全档周限 / 首页显示全部已兑：当天转收菜


def configure_logging():
    """只在作为程序入口时配置日志——import 时不碰，避免测试日志写进生产日志。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def retry_interval(attempt, base, cap):
    """第 attempt 次失败后的等待秒数：指数递增（base, base*2, base*4…），cap 封顶。
    默认 base=1, cap=15 → 1, 2, 4, 8, 15, 15…"""
    return min(cap, base * (2 ** (attempt - 1)))


def run_once(cfg, gateway=None, clock=None, rng=random, sleep_fn=time.sleep):
    """执行「狙击等待→查询→级联兑换」，全部售罄按配置递增重试。

    返回 DailyRunResult：SUCCESS / WEEK_COMPLETE（全档周限或首页全已兑，当天
    立即结束重试）/ FAILED。SessionExpired / RiskControlBlocked 直接抛出。"""
    gateway = gateway or TaoCoinGateway(cfg.cookie)

    if clock is not None and cfg.snipe_time:
        clock.wait_for(cfg.snipe_time)

    ordered = daily_tier_order(cfg.tiers, rng, strategy=cfg.tier_strategy)
    log.info("今日档位优先级（%s）: %s", cfg.tier_strategy,
             " → ".join(t["label"] for t in ordered))

    for attempt in range(1, cfg.exchange_retries + 1):
        snapshot = gateway.fetch_benefits()
        log.info("可兑换列表共 %d 项", len(snapshot.benefits))

        if snapshot.exchanged_all:
            log.info("今天红包已全部兑换过，无需操作")
            return DailyRunResult.WEEK_COMPLETE

        # cascade 只认原始字典（关键词匹配在整个 JSON 上）
        outcome = cascade_exchange(
            [b.raw for b in snapshot.benefits], ordered, gateway.exchange)

        if outcome.success:
            return DailyRunResult.SUCCESS
        if outcome.all_week_limited:
            # 所有匹配档位都报「已达周限」：重试无意义，当天直接转收菜
            log.info("所有匹配档位均已达到本周兑换上限，今日转收菜")
            return DailyRunResult.WEEK_COMPLETE
        if not outcome.attempts:
            # 一档都没匹配上：配置问题，重试无意义
            return DailyRunResult.FAILED

        if attempt < cfg.exchange_retries:
            wait = retry_interval(attempt, cfg.exchange_retry_base,
                                  cfg.exchange_retry_max)
            log.info("第 %d/%d 轮未兑换成功，%.0f 秒后重试…",
                     attempt, cfg.exchange_retries, wait)
            sleep_fn(wait)

    log.error("今日兑换未成功（已试 %d 轮）", cfg.exchange_retries)
    return DailyRunResult.FAILED


def run_daily_flow(cfg, gateway=None, clock=None, rng=random, sleep_fn=time.sleep):
    """常驻循环与 --once 的完整日常流程：先读探测判定当日模式（狙击日/收菜日），
    再分别执行「狙击+收取」或「抖动+收取」。无跨日状态文件，每天由当天首页
    数据重新判定；首页权益恢复（新一周）即自动回到狙击日。

    收取在兑换流程正常结束后立刻执行（成功/重试耗尽/全档周限都算正常结束）；
    SessionExpired / RiskControlBlocked 从兑换路径抛出时跳过收取，交给常驻
    循环的外层网。返回值与退出码只看兑换侧，收取失败只记日志。"""
    gateway = gateway or TaoCoinGateway(cfg.cookie)

    # 读探测：模式判定的唯一依据（read-only，不消耗兑换机会），run_time 准点发生
    snapshot = gateway.fetch_benefits()
    if snapshot.exchanged_all:
        log.info("首页显示当周红包已全部兑换，今日为收菜日")
        return _collection_day(gateway, rng, sleep_fn)

    result = run_once(cfg, gateway=gateway, clock=clock, rng=rng, sleep_fn=sleep_fn)
    # 狙击日：兑换流程正常结束后立刻收取，时刻天然不规则，不加抖动
    _log_collection_summary(run_daily_collection(gateway))
    return result


def _collection_day(gateway, rng, sleep_fn):
    """收菜日：不再重试兑换，只做一次每日收取。抖动只加在收取动作上（避免每天
    同一时刻的请求画像），醒来的读探测仍对准 run_time。"""
    wait_seconds = rng.uniform(0, COLLECTION_JITTER_MINUTES) * 60
    log.info("收菜日收取动作抖动 %.1f 分钟后执行", wait_seconds / 60)
    if wait_seconds:
        sleep_fn(wait_seconds)
    result = run_daily_collection(gateway)
    _log_collection_summary(result)
    return DailyRunResult.WEEK_COMPLETE


def _log_collection_summary(result):
    """每日收取收尾：摘要行两种模式共用（绑定格式，不重排）；
    唯独登录态失效要在当日摘要里显著标记。"""
    log.info("%s", result.summary())
    if result.session_expired:
        log.error("🔑 每日收取发现登录态失效，请更新 config.json 里的 cookie")


def list_only(cfg):
    """--list：只打印当前可兑换列表。"""
    snapshot = TaoCoinGateway(cfg.cookie).fetch_benefits()
    print("exchanged_all =", snapshot.exchanged_all)
    for i, b in enumerate(snapshot.benefits):
        print("[{}] {} | {}金币 | code: {}".format(i, b.title, b.coin_amount, b.code))


def collect_once(cfg):
    """--collect：手动执行一次每日收取并打印摘要行。"""
    result = run_daily_collection(TaoCoinGateway(cfg.cookie))
    _log_collection_summary(result)


def main():
    now_mode = "--now" in sys.argv
    once_mode = "--once" in sys.argv
    list_mode = "--list" in sys.argv
    collect_mode = "--collect" in sys.argv

    configure_logging()
    cfg = Config.load(CONFIG_PATH)

    if list_mode:
        list_only(cfg)
        return

    if collect_mode:
        collect_once(cfg)
        return

    if now_mode:
        # --now 保持只测兑换路径（狙击等待+级联），不跑每日收取
        run_once(cfg)
        return

    if once_mode:
        # 供 launchd 调用：一次完整的「狙击等待+级联重试+每日收取」日常流程，然后退出
        ok = run_daily_flow(cfg, clock=SnipeClock())
        sys.exit(0 if ok else 1)

    clock = SnipeClock()
    log.info("进入常驻模式，每天 %s 启动、%s 准点狙击（Ctrl+C 退出）",
             cfg.run_time, cfg.snipe_time)

    while True:
        nxt = clock.next_run(cfg.run_time)
        log.info("下次运行: %s", nxt.strftime("%Y-%m-%d %H:%M:%S"))

        while datetime.now() < nxt:
            time.sleep(min(30, (nxt - datetime.now()).total_seconds()))

        try:
            cfg = Config.load(CONFIG_PATH)  # 每天重读配置，改 cookie/时间不用重启
            run_daily_flow(cfg, clock=clock)
        except SessionExpired as e:
            log.error("🔑 %s", e)
        except RiskControlBlocked as e:
            log.warning("🛡️ %s", e)
        except Exception as e:  # noqa: BLE001 - 常驻循环不能让单次异常退出
            log.exception("本次执行出现异常: %s", e)

        # 防止 run_time 恰好在执行完成的同一分钟导致立刻再来一轮
        time.sleep(61)


if __name__ == "__main__":
    main()
