#!/usr/bin/env python3
"""cascade module：档位级联。按当日优先级逐档尝试兑换，失败落下一档。

interface 只有一个 cascade_exchange(benefits, tiers, exchange_fn)；
exchange_fn 是 seam：生产注 gateway.exchange，测试注 scripted stub。
级联契约：ExchangeFailed 落下一档；SessionExpired / RiskControlBlocked 直接抛出（当天中止）。
"""

import logging
import random
from dataclasses import dataclass, field

from gateway import ExchangeFailed

log = logging.getLogger("cascade")


@dataclass
class ExchangeOutcome:
    """一轮级联的结果。attempts 记录每档的 (label, 结果描述)。"""
    success: bool
    tier_label: str = None
    award: dict = None
    attempts: list = field(default_factory=list)


def benefit_matches_tier(item, tier):
    """在语义字段（标题、面额、消耗金币）上命中 tier 的任一 keyword 即匹配。

    不做整段 JSON 子串匹配：无关字段会误命中——真实接口里每个权益的
    图床 URL 都含 "6000000000…"，曾让 10元项命中 20元档的 "6000" 关键词。"""
    text = "{} {}{} {}".format(
        item.get("displayTitle", ""),
        item.get("displayAmount", ""),
        item.get("displayAmountUnit", ""),
        item.get("reduceCoinAmount", ""),
    )
    return any(kw in text for kw in tier["keywords"])


def daily_tier_order(tiers, rng=random, strategy="random"):
    """每天的档位尝试顺序。

    strategy="fixed"：严格按 tiers 数组顺序（配置的优先级即尝试顺序）。
    strategy="random"：前两档（大额档）随机先后，最后一档兜底。
    """
    if strategy == "fixed":
        return list(tiers)
    if strategy != "random":
        raise ValueError("strategy 只能是 'random' 或 'fixed': {}".format(strategy))
    if len(tiers) <= 2:
        order = list(tiers)
        rng.shuffle(order)
        return order
    big = list(tiers[:2])
    rng.shuffle(big)
    return big + list(tiers[2:])


def cascade_exchange(benefits, ordered_tiers, exchange_fn):
    """按优先级逐档尝试。全部失败返回 success=False 的 ExchangeOutcome；
    SessionExpired / RiskControlBlocked 不捕获，直接向上抛出。"""
    attempts = []
    for tier in ordered_tiers:
        item = next((b for b in benefits if benefit_matches_tier(b, tier)), None)
        if not item:
            log.info("【%s】无可匹配权益，跳过", tier["label"])
            continue
        code = item.get("benefitCode")
        if not code:
            log.warning("【%s】匹配项缺少 benefitCode，跳过", tier["label"])
            attempts.append((tier["label"], "缺少 benefitCode"))
            continue

        log.info("尝试兑换【%s】(benefitCode: %s…)…", tier["label"], code[:12])
        try:
            award = exchange_fn(code)
        except ExchangeFailed as e:
            log.warning("【%s】失败: %s → 落到下一档", tier["label"], e)
            attempts.append((tier["label"], str(e)))
            continue

        # 成功日志以服务端返回的实际面额为准，不只报档位标签：
        # 2026-09-30 曾因匹配错位把 10元记成「✅ 兑换成功【20元红包】」
        if isinstance(award, dict) and award.get("displayAmount"):
            log.info("✅ 兑换成功【%s】实际到账 %s%s", tier["label"],
                     award["displayAmount"], award.get("displayAmountUnit", ""))
        else:
            log.info("✅ 兑换成功【%s】", tier["label"])
        attempts.append((tier["label"], "成功"))
        return ExchangeOutcome(success=True, tier_label=tier["label"],
                               award=award, attempts=attempts)

    if not attempts:
        log.warning("没有匹配到任何目标档位，请检查 config.json 的 tiers 关键词")
    return ExchangeOutcome(success=False, attempts=attempts)
