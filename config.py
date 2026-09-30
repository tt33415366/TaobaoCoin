#!/usr/bin/env python3
"""Config module：加载、默认值解析、校验一次完成，调用方只读属性。"""

import json
import logging
from pathlib import Path

log = logging.getLogger("config")

# 登录态关键 cookie：缺失时接口必返回 Session过期
REQUIRED_COOKIE_KEYS = ("cookie2", "_m_h5_tk", "_tb_token_")

DEFAULT_TIERS = [
    {"label": "20元红包", "keywords": ["20元", "6000"]},
    {"label": "10元红包", "keywords": ["10元", "3000"]},
    {"label": "5元红包", "keywords": ["5元", "1500"]},
]

_DEFAULTS = {
    "run_time": "09:59:30",
    "snipe_time": "10:00:00",
    "exchange_retries": 14,
    "exchange_retry_base": 1,
    "exchange_retry_max": 15,
}


class Config:
    """默认值已解析、校验已完成的配置。属性直达，不再有 cfg.get(..., default)。"""

    def __init__(self, cookie, run_time, snipe_time, exchange_retries,
                 exchange_retry_base, exchange_retry_max, tiers):
        self.cookie = cookie
        self.run_time = run_time
        self.snipe_time = snipe_time
        self.exchange_retries = exchange_retries
        self.exchange_retry_base = exchange_retry_base
        self.exchange_retry_max = exchange_retry_max
        self.tiers = tiers

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                "找不到 {}，请参照 config.example.json 创建并填入 cookie".format(path)
            )
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)

        cookie = raw.get("cookie", "")
        if "把你的cookie粘贴到这里" in cookie or not cookie:
            raise ValueError("请先在 config.json 里填入真实的淘宝 cookie")
        missing = [k for k in REQUIRED_COOKIE_KEYS if k not in cookie]
        if missing:
            log.warning(
                "⚠️ cookie 缺少关键字段: %s。请从发往 h5api.m.taobao.com 的接口请求里"
                "复制整串 Cookie（页面文档请求的 cookie 不带登录态）", ", ".join(missing)
            )

        return cls(
            cookie=cookie,
            run_time=raw.get("run_time", _DEFAULTS["run_time"]),
            snipe_time=raw.get("snipe_time", _DEFAULTS["snipe_time"]),
            exchange_retries=int(raw.get("exchange_retries", _DEFAULTS["exchange_retries"])),
            exchange_retry_base=float(raw.get("exchange_retry_base", _DEFAULTS["exchange_retry_base"])),
            exchange_retry_max=float(raw.get("exchange_retry_max", _DEFAULTS["exchange_retry_max"])),
            tiers=raw.get("tiers") or list(DEFAULT_TIERS),
        )
