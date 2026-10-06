#!/usr/bin/env python3
"""每日收取 module：兑换之外的日常维护动作编排（CONTEXT.md：每日收取 DailyCollection）。

interface 只有一个 run_daily_collection(gateway)，返回 DailyCollectionResult
（本次获得金币数、收尾余额、登录态是否失效）。常驻循环与 --collect 都直接调它。

流程（2026-10-02 实测校准后）：
    1. 查金币小镇状态（town.index.get.pc）：余额 + 今日是否已签
       ——余额的唯一可靠来源；首页 queryTaoCoinHomeV2 不带余额
    2. 已签则记「今日已签到」跳过；未签才 签到+收币（collect.reward.pc）
       再 签到状态同步（pcSign4Sync，模仿页面行为）
    3. 收尾再查一次小镇状态，+X 取余额差——免疫金币字段名猜测；
       起始状态没查到才退化为签到接口自报的 reward

失败语义（与 CONTEXT.md 一致）：收取的任何失败只记日志，绝不抛出 fatal——
当日已签到是正常的业务结局；风控跳过该动作继续；唯独 SessionExpired 中止
（登录态已死，后续必然同样失败），但不抛异常，而是记进
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
    """执行一轮每日收取，逐个动作做、失败隔离，返回 DailyCollectionResult。"""
    result = DailyCollectionResult(coins_gained=0, balance=0)

    # 1. 起始小镇状态：余额 + 是否已签。查挂了不致命——仍尝试签到，+X 退化
    try:
        before = gateway.query_coin_town()
        result.balance = before.balance
    except SessionExpired as e:
        log.error("🔑 查询金币小镇状态时登录态失效: %s", e)
        result.session_expired = True
        return result
    except Exception as e:  # noqa: BLE001 - 起始状态只是参照，失败不致命
        log.warning("查询金币小镇状态失败，仍尝试签到（+X 按签到接口自报记）: %s", e)
        before = None

    # 2. 签到：已签跳过（实测已签时 collect.reward.pc 返回空 data，属正常结局）
    reward_from_sign = 0
    if before is not None and before.signed:
        log.info("今日已签到，跳过签到动作")
    else:
        # 未签才做页面热身（2026-10-07 实测校准）：页面挂载时先调
        # town.index + queryTaoCoinHomeV2 再发签到。复刻这个顺序后签到成功
        # （此前无热身且多带 params 的调用被服务端静默吞掉：SUCCESS 但空 data）。
        # 热身失败不阻塞签到；已签跳过则连热身也不发（无意义请求不发）。
        try:
            gateway.fetch_benefits()
        except Exception as e:  # noqa: BLE001 - 热身只是会话铺垫，失败不致命
            log.warning("签到前首页热身失败，仍尝试签到: %s", e)
        for label, action in (("签到+收币", gateway.collect_sign_reward),
                              ("签到状态同步", gateway.sync_sign_status)):
            try:
                gained = action()
            except SessionExpired as e:
                # 登录态失效：后续动作必然同样失败，中止；不抛出，交给调用方标记
                log.error("🔑 【%s】登录态失效，每日收取中止: %s", label, e)
                result.session_expired = True
                break
            except RiskControlBlocked as e:
                log.warning("🛡️ 【%s】触发风控，跳过: %s", label, e)
            except ExchangeFailed as e:
                log.info("【%s】未获得金币（%s）", label, e)
            except Exception as e:  # noqa: BLE001 - 收取失败绝不 fatal
                log.exception("【%s】执行异常: %s", label, e)
            else:
                reward_from_sign += gained or 0
                if gained:
                    log.info("【%s】+%d 金币", label, gained)

    # 3. 收尾小镇状态：+X 优先取余额差；收尾查挂了用签到接口自报的数
    result.coins_gained = reward_from_sign
    if not result.session_expired:
        try:
            after = gateway.query_coin_town()
            result.balance = after.balance
            if before is not None:
                result.coins_gained = after.balance - before.balance
        except SessionExpired as e:
            log.error("🔑 收取收尾查余额时登录态失效: %s", e)
            result.session_expired = True
        except Exception as e:  # noqa: BLE001 - 余额只是摘要展示，失败不致命
            log.warning("收取收尾查余额失败: %s", e)

    return result
