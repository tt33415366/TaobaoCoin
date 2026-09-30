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
"""

import logging
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from cascade import cascade_exchange, daily_tier_order
from config import Config
from gateway import ExchangeFailed, RiskControlBlocked, SessionExpired, TaoCoinGateway
from snipe_clock import SnipeClock

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "taobao_coin.log"

log = logging.getLogger("taobao_coin")


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
    """执行「狙击等待→查询→级联兑换」，全部售罄按配置递增重试。返回 True 表示成功。"""
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
            return True

        # cascade 只认原始字典（关键词匹配在整个 JSON 上）
        outcome = cascade_exchange(
            [b.raw for b in snapshot.benefits], ordered, gateway.exchange)

        if outcome.success:
            return True
        if not outcome.attempts:
            # 一档都没匹配上：配置问题，重试无意义
            return False

        if attempt < cfg.exchange_retries:
            wait = retry_interval(attempt, cfg.exchange_retry_base,
                                  cfg.exchange_retry_max)
            log.info("第 %d/%d 轮未兑换成功，%.0f 秒后重试…",
                     attempt, cfg.exchange_retries, wait)
            sleep_fn(wait)

    log.error("今日兑换未成功（已试 %d 轮）", cfg.exchange_retries)
    return False


def list_only(cfg):
    """--list：只打印当前可兑换列表。"""
    snapshot = TaoCoinGateway(cfg.cookie).fetch_benefits()
    print("exchanged_all =", snapshot.exchanged_all)
    for i, b in enumerate(snapshot.benefits):
        print("[{}] {} | {}金币 | code: {}".format(i, b.title, b.coin_amount, b.code))


def main():
    now_mode = "--now" in sys.argv
    once_mode = "--once" in sys.argv
    list_mode = "--list" in sys.argv

    configure_logging()
    cfg = Config.load(CONFIG_PATH)

    if list_mode:
        list_only(cfg)
        return

    if now_mode:
        run_once(cfg)
        return

    if once_mode:
        # 供 launchd 调用：一次完整的「狙击等待+级联重试」日常流程，然后退出
        ok = run_once(cfg, clock=SnipeClock())
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
            run_once(cfg, clock=clock)
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
