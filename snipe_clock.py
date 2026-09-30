#!/usr/bin/env python3
"""SnipeClock module：狙击时钟。让兑换请求对准每日库存刷新的狙击时刻（10:00:00）。

interface 只有一个 wait_for(snipe_time)；时钟校准、提前量、窗口边界全部藏在
implementation 里。时间源（now/sleep/offset）是内部 seam，只给测试注入用。
"""

import json
import logging
import time
from datetime import datetime, timedelta
from urllib.request import Request, build_opener

log = logging.getLogger("snipe_clock")

LEAD_SECONDS = 0.6   # 一次「查询+兑换」约 0.6s，提前发起让兑换请求准点抵达服务器
SNIPE_WINDOW = 600   # 距狙击时刻超过 10 分钟则不等待（防配置错误挂死）

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def parse_hms(time_str):
    """"HH:MM" 或 "HH:MM:SS" → (h, m, s)。"""
    try:
        parts = [int(x) for x in time_str.split(":")]
    except ValueError:
        raise ValueError("时间格式应为 HH:MM 或 HH:MM:SS: {}".format(time_str))
    if len(parts) == 2:
        parts.append(0)
    if len(parts) != 3:
        raise ValueError("时间格式应为 HH:MM 或 HH:MM:SS: {}".format(time_str))
    return parts[0], parts[1], parts[2]


def measure_clock_offset():
    """用 mtop.common.getTimestamp 取服务器时间，估算 (服务器时钟 - 本地时钟) 秒数。
    该接口无需签名和登录。失败返回 0.0（就当没偏差，不影响流程）。
    注意不要用页面 URL 的 Date 头校准——CDN 缓存页面会带过期 Date。"""
    url = "https://h5api.m.taobao.com/h5/mtop.common.getTimestamp/1.0/"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    t0 = time.time()
    try:
        with build_opener().open(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        t1 = time.time()
        server_ts = int(payload["data"]["t"]) / 1000
    except Exception:
        return 0.0
    # 用请求中点近似本地时刻，抵消一半网络延迟
    return server_ts - (t0 + t1) / 2


class SnipeClock:
    """狙击时钟。wait_for 睡到「狙击时刻 - 时钟偏差 - 提前量」再返回。"""

    def __init__(self, now_fn=None, sleep_fn=None, offset_fn=None):
        self._now = now_fn or datetime.now
        self._sleep = sleep_fn or time.sleep
        self._offset = offset_fn or measure_clock_offset

    def next_run(self, run_time_str):
        """下一次日常运行时刻：今天 run_time，若已过则为明天。"""
        hh, mm, ss = parse_hms(run_time_str)
        now = self._now()
        candidate = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    def wait_for(self, snipe_time_str):
        """校准时钟后睡到狙击时刻前 LEAD_SECONDS。仅在窗口内时才等待。"""
        offset = self._offset()
        hh, mm, ss = parse_hms(snipe_time_str)
        snipe_dt = self._now().replace(hour=hh, minute=mm, second=ss, microsecond=0)
        wait = (snipe_dt - self._now()).total_seconds() - offset - LEAD_SECONDS
        if 0 < wait <= SNIPE_WINDOW:
            log.info("时钟偏差 %+.2fs，%.1f 秒后开第一枪（兑换请求对准服务器 %s）",
                     offset, wait, snipe_time_str)
            self._sleep(wait)
        else:
            log.info("不在狙击窗口内（%s），立即开始", snipe_time_str)
