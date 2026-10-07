#!/usr/bin/env python3
"""
淘宝淘金币 - 每天自动兑换红包（薄入口）

编排各 module：
  config.Config      配置加载与校验
  snipe_clock        狙击时钟（准点对准 10:00 库存刷新）
  gateway            淘金币 Gateway（mtop 协议细节全在里面）
  day_mode           当日模式（狙击日/收菜日/纯收取日的推导与执行；
                     档位级联 cascade 由它编排）
  collect            每日收取（DailyCollection 的业务语义与失败隔离）

用法：
  python3 taobao_coin.py            # 常驻，每天到 config.json 里的 run_time 执行
  python3 taobao_coin.py --now      # 立刻执行一次兑换路径（测试用，不读 features）
  python3 taobao_coin.py --list     # 只拉取并打印当前可兑换列表，不兑换
  python3 taobao_coin.py --collect  # 立刻执行一次每日收取（签到）
"""

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from config import Config
from day_mode import collect_and_summarize, run_day, run_exchange
from gateway import RiskControlBlocked, SessionExpired, TaoCoinGateway
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


def exit_code(result):
    """--once 的退出码（launchd 依赖）：仅狙击日兑换失败（重试耗尽且无成功、
    非全档周限）为 1，其余（兑换成功 / 收菜日 / 纯收取日 / 全档周限）皆 0。"""
    ex = result.exchange
    if ex is not None and not ex.success and not ex.week_complete:
        return 1
    return 0


def list_only(cfg):
    """--list：只打印当前可兑换列表。"""
    snapshot = TaoCoinGateway(cfg.cookie).fetch_benefits()
    print("exchanged_all =", snapshot.exchanged_all)
    for i, b in enumerate(snapshot.benefits):
        print("[{}] {} | {}金币 | code: {}".format(i, b.title, b.coin_amount, b.code))


def collect_once(cfg):
    """--collect：手动执行一次每日收取并打印摘要行。"""
    collect_and_summarize(TaoCoinGateway(cfg.cookie))


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
        if not cfg.collect_enabled:
            log.info("提示：features.collect=false，--collect 是手动显式调用，仍执行")
        collect_once(cfg)
        return

    if now_mode:
        if not cfg.exchange_enabled:
            log.info("提示：features.exchange=false，--now 是手动显式调用，仍执行")
        # --now 保持只测兑换路径（级联），不跑每日收取、不读 features；
        # 立刻执行——不传 clock，不做狙击等待
        run_exchange(cfg)
        return

    if once_mode:
        # 供 launchd 调用：一次完整的「狙击等待+级联重试+每日收取」日常流程，然后退出
        sys.exit(exit_code(run_day(cfg, clock=SnipeClock())))

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
            run_day(cfg, clock=clock)
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
