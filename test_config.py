#!/usr/bin/env python3
"""config.py 的测试。运行: python3 test_config.py"""

import json
import tempfile
import unittest
from pathlib import Path

from config import Config

GOOD_COOKIE = "cookie2=x; _m_h5_tk=tok_123; _tb_token_=y; other=z"


def write_cfg(tmpdir, data):
    p = Path(tmpdir) / "config.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


class TestConfigLoad(unittest.TestCase):
    def test_defaults_applied_when_optional_keys_missing(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_cfg(d, {"cookie": GOOD_COOKIE})
            cfg = Config.load(path)
        self.assertEqual(cfg.cookie, GOOD_COOKIE)
        self.assertEqual(cfg.run_time, "09:59:30")
        self.assertEqual(cfg.snipe_time, "10:00:00")
        self.assertEqual(cfg.exchange_retries, 14)
        self.assertEqual(cfg.exchange_retry_base, 1)
        self.assertEqual(cfg.exchange_retry_max, 15)
        self.assertEqual(cfg.tier_strategy, "random")
        self.assertEqual([t["label"] for t in cfg.tiers],
                         ["20元红包", "10元红包", "5元红包"])

    def test_explicit_values_win_over_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_cfg(d, {
                "cookie": GOOD_COOKIE,
                "run_time": "08:00",
                "exchange_retries": 3,
                "tier_strategy": "fixed",
                "tiers": [{"label": "X", "keywords": ["k"]}],
            })
            cfg = Config.load(path)
        self.assertEqual(cfg.run_time, "08:00")
        self.assertEqual(cfg.exchange_retries, 3)
        self.assertEqual(cfg.tier_strategy, "fixed")
        self.assertEqual(cfg.tiers, [{"label": "X", "keywords": ["k"]}])

    def test_invalid_tier_strategy_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_cfg(d, {"cookie": GOOD_COOKIE, "tier_strategy": "bogus"})
            with self.assertRaises(ValueError):
                Config.load(path)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            Config.load("/nonexistent/config.json")

    def test_placeholder_cookie_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_cfg(d, {"cookie": "把你的cookie粘贴到这里"})
            with self.assertRaises(ValueError):
                Config.load(path)

    def test_incomplete_cookie_warns(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_cfg(d, {"cookie": "aui=1; cna=2"})
            with self.assertLogs("config", level="WARNING") as cm:
                Config.load(path)
        self.assertTrue(any("cookie2" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
