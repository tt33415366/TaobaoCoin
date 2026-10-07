#!/usr/bin/env python3
"""每日收取 module：兑换之外的日常维护动作（CONTEXT.md：每日收取 DailyCollection）。

interface 只有一个 run_daily_collection(gateway)，返回 DailyCollectionResult
（本次获得金币数、收尾余额、登录态是否失效）。常驻循环与 --collect 都直接调它。

流程（ADR 0002 后）：
    1. gateway.sign_in()——页面挂载序列（town→已签短路→home热身→签到→同步）
       是协议细节，由 gateway 独家持有；本模块只见 SignOutcome
    2. 收尾再查一次小镇状态（query_coin_town）：+X 取余额差——免疫金币字段名
       猜测；起始余额没查到（balance_before=None）才退化为签到自报的 reward

失败语义（与 CONTEXT.md 一致）：收取的任何失败只记日志，绝不抛出 fatal——
当日已签到是正常的业务结局；风控跳过签到、收尾测量照常；唯独 SessionExpired
中止（登录态已死，后续必然同样失败），但不抛异常，而是记进
result.session_expired，由调用方在当日摘要里显著标记。
"""

import logging
from dataclasses import dataclass

from gateway import ExchangeFailed, RiskControlBlocked, SessionExpired

log = logging.getLogger("collect")


@dataclass
class DailyCollectionResult:
    """一轮每日收取的结果。session_expired 由调用方在当日摘要里显著标记。"""
    coins_gained: int
    balance: int
    session_expired: bool = False

    def summary(self):
        """当日摘要行（绑定格式）：「今日收取 +X，余额 Y」。"""
        return "今日收取 +{}，余额 {}".format(self.coins_gained, self.balance)


def run_daily_collection(gateway):
    """执行一轮每日收取，失败隔离，返回 DailyCollectionResult。"""
    result = DailyCollectionResult(coins_gained=0, balance=0)
    reward_from_sign = 0
    balance_before = None

    # 1. 签到：页面挂载序列是 gateway 的内部知识；已签短路由 SignOutcome.skipped 表达
    try:
        outcome = gateway.sign_in()
    except SessionExpired as e:
        # 登录态失效：收尾查询必然同样失败，中止；不抛出，交给调用方标记
        log.error("🔑 每日收取发现登录态失效: %s", e)
        result.session_expired = True
        return result
    except RiskControlBlocked as e:
        log.warning("🛡️ 签到触发风控，跳过: %s", e)
    except ExchangeFailed as e:
        log.info("签到未获得金币（%s）", e)
    except Exception:  # noqa: BLE001 - 收取失败绝不 fatal
        log.exception("签到执行异常")
    else:
        reward_from_sign = outcome.reward
        balance_before = outcome.balance_before
        if outcome.skipped:
            log.info("今日已签到，跳过签到动作")
        elif outcome.reward:
            log.info("【签到+收币】+%d 金币", outcome.reward)
        if balance_before is not None:
            result.balance = balance_before

    # 2. 收尾小镇状态（+X 测量的后半截）：+X 优先取余额差；收尾查挂了用自报数
    result.coins_gained = reward_from_sign
    try:
        after = gateway.query_coin_town()
        result.balance = after.balance
        if balance_before is not None:
            result.coins_gained = after.balance - balance_before
    except SessionExpired as e:
        log.error("🔑 收取收尾查余额时登录态失效: %s", e)
        result.session_expired = True
    except Exception as e:  # noqa: BLE001 - 余额只是摘要展示，失败不致命
        log.warning("收取收尾查余额失败: %s", e)

    return result
