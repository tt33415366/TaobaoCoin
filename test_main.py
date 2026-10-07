#!/usr/bin/env python3
"""taobao_coin.py 入口的测试。运行: python3 test_main.py

入口层只剩一张皮：arg 解析、日志配置、常驻循环接线。唯一自有契约是
--once 的退出码映射（launchd 依赖）——仅狙击日兑换失败退出码 1。
当日模式的全部行为规格在 test_day_mode.py。"""

import unittest

from collect import DailyCollectionResult
from day_mode import DayResult, ExchangeResult, Mode
from taobao_coin import exit_code


class TestExitCode(unittest.TestCase):
    def test_snipe_day_exchange_failed_exits_1(self):
        result = DayResult(mode=Mode.SNIPE_DAY,
                           exchange=ExchangeResult(success=False))
        self.assertEqual(exit_code(result), 1)

    def test_snipe_day_success_exits_0(self):
        result = DayResult(mode=Mode.SNIPE_DAY,
                           exchange=ExchangeResult(success=True))
        self.assertEqual(exit_code(result), 0)

    def test_week_complete_exits_0(self):
        # 全档周限/首页全已兑是正常结局，不算失败
        result = DayResult(mode=Mode.SNIPE_DAY,
                           exchange=ExchangeResult(success=False,
                                                   week_complete=True))
        self.assertEqual(exit_code(result), 0)

    def test_collection_day_exits_0(self):
        result = DayResult(mode=Mode.COLLECTION_DAY,
                           collection=DailyCollectionResult(0, 100))
        self.assertEqual(exit_code(result), 0)

    def test_pure_collection_day_exits_0(self):
        result = DayResult(mode=Mode.PURE_COLLECTION_DAY,
                           collection=DailyCollectionResult(0, 100))
        self.assertEqual(exit_code(result), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
