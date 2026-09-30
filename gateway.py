#!/usr/bin/env python3
"""淘金币 Gateway：与淘宝淘金币活动服务端交互的唯一通道。

interface 收窄到领域语言：
    fetch_benefits() -> HomeSnapshot   查可兑换权益列表
    exchange(code)   -> dict           按 benefitCode 兑换，返回奖品

implementation 藏住：mtop H5 签名、_m_h5_tk token 刷新、API 名、asac 常量、
data.data 双层拆包、ret 错误分类。MtopClient 是内部 transport（私有 seam），
测试通过 transport 参数注入假 adapter。

异常契约：ExchangeFailed 可级联；SessionExpired / RiskControlBlocked 当天中止。
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from http.cookiejar import CookieJar
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

log = logging.getLogger("gateway")

APP_KEY = "12574478"  # lib-mtop 对 taobao.com 的默认 appKey
API_VERSION = "1.0"   # 实测网关不接受 "*"，URL 路径里必须是具体版本号
MTOP_ENDPOINT = "https://h5api.m.taobao.com/h5/{api}/{v}/"

API_HOME = "mtop.taobao.pc.growth.taocoin.queryTaoCoinHomeV2"
API_EXCHANGE = "mtop.taobao.pc.growth.taocoin.exchangeBenefit"

# 从页面 JS（p_gold-index.js）逆向得到的固定参数
ASAC_HOME = "2A24C24PP4OZC3YF9XCDIA"
ASAC_EXCHANGE = "2A24A17A33HG02DHKF2BEX"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------- 异常契约

class SessionExpired(Exception):
    """登录态失效：需要重新粘贴 cookie。不属于级联，当天立即中止。"""


class RiskControlBlocked(Exception):
    """触发风控（滑块/验证）：本次放弃。不属于级联，当天立即中止。"""


class ExchangeFailed(Exception):
    """业务层兑换失败（如库存已空、已达周限、金币不足）：级联到下一档。"""


# ---------------------------------------------------------------- 领域对象

@dataclass
class Benefit:
    """一条可兑换权益。raw 保留原始字典供关键词匹配。"""
    code: str
    title: str
    coin_amount: int
    raw: dict


@dataclass
class HomeSnapshot:
    exchanged_all: bool
    benefits: list


# ---------------------------------------------------------------- 纯函数

def extract_token(cookie_str):
    """从 cookie 字符串中取 _m_h5_tk 的 token 部分（下划线前半段）。取不到返回 None。"""
    m = re.search(r"(?:^|;\s*)_m_h5_tk=([^;]+)", cookie_str)
    if not m:
        return None
    return m.group(1).split("_")[0]


def mtop_sign(token, t, app_key, data):
    """mtop H5 标准签名：md5(token&t&appKey&data)，小写 hex。"""
    raw = "{}&{}&{}&{}".format(token, t, app_key, data)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def update_cookie(cookie_str, name, value):
    """替换 cookie 串里的某个值；不存在则追加。"""
    if re.search(r"(?:^|;\s*)" + re.escape(name) + r"=", cookie_str):
        return re.sub(re.escape(name) + r"=[^;]+", name + "=" + value, cookie_str)
    return cookie_str + "; " + name + "=" + value


# ---------------------------------------------------------------- 内部 transport

class MtopClient:
    """mtop 协议 transport：签名、token 自刷新、错误分类。Gateway 的私有实现细节。"""

    def __init__(self, cookie_str):
        self.cookie_str = cookie_str
        self.cj = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cj))
        # 把整串 cookie 直接放 header；_m_h5_tk 刷新通过 Set-Cookie 落到 CookieJar
        self._token = extract_token(cookie_str)

    def _refresh_token_from_jar(self):
        # _m_h5_tk 和 _m_h5_tk_enc 是配对校验的，必须一起换新，
        # 否则 FAIL_SYS_TOKEN_ILLEGAL。同时 urllib 在已有 Cookie 头时不会
        # 合并 CookieJar 的新值，所以要显式改写 cookie_str。
        jar = {c.name: c.value for c in self.cj}
        if "_m_h5_tk" not in jar:
            return False
        self._token = jar["_m_h5_tk"].split("_")[0]
        self.cookie_str = update_cookie(self.cookie_str, "_m_h5_tk", jar["_m_h5_tk"])
        if "_m_h5_tk_enc" in jar:
            self.cookie_str = update_cookie(self.cookie_str, "_m_h5_tk_enc",
                                            jar["_m_h5_tk_enc"])
        return True

    def request(self, api, data_dict):
        """调一次 mtop 接口，返回 (ret列表, data)。token 过期自动换新重试一次。"""
        data = json.dumps(data_dict, ensure_ascii=False, separators=(",", ":"))
        for attempt in range(2):
            t = str(int(time.time() * 1000))
            sign = mtop_sign(self._token or "", t, APP_KEY, data)
            params = {
                "jsv": "2.7.2",
                "appKey": APP_KEY,
                "t": t,
                "sign": sign,
                "api": api,
                "v": API_VERSION,
                "dataType": "json",
                "type": "originaljson",
                "data": data,
            }
            url = MTOP_ENDPOINT.format(api=api, v=API_VERSION)
            # 与浏览器一致用 POST 表单提交（lib-mtop 对这类接口 type 为 POST）
            req = Request(url, data=urlencode(params).encode("utf-8"), headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://huodong.taobao.com/",
                "Origin": "https://huodong.taobao.com",
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": self.cookie_str,
            })
            try:
                with self.opener.open(req, timeout=15) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
            except URLError as e:
                raise ConnectionError("网络请求失败: {}".format(e))

            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                raise RuntimeError("返回非 JSON（可能被风控拦截）: {}".format(body[:200]))

            ret = payload.get("ret", [])
            ret_text = ";".join(ret)

            if any("SUCCESS" in r for r in ret):
                return ret, payload.get("data")

            if any("TOKEN_EXOIRED" in r or "TOKEN_EMPTY" in r or "TOKEN_EXPIRED" in r for r in ret):
                # token 失效：mtop 已在 Set-Cookie 下发新 _m_h5_tk，换新后重试一次
                if attempt == 0 and self._refresh_token_from_jar():
                    log.info("mtop token 已刷新，重试")
                    continue
                raise SessionExpired("token 刷新失败，请更新 config.json 里的 cookie")

            if any("SESSION_EXPIRED" in r or "ILLEGAL_ACCESS" in r for r in ret):
                raise SessionExpired("登录态失效（{}），请重新从浏览器复制 cookie".format(ret_text))

            if any("USER_VALIDATE" in r or "ACCESS_DENIED" in r or "RGV587" in r for r in ret):
                raise RiskControlBlocked("触发淘宝风控/滑块（{}），本次放弃".format(ret_text))

            # 其余失败：优先透出业务层 message（如「权益已变更」=库存已空）
            inner = payload.get("data") or {}
            inner_msg = inner.get("message") if isinstance(inner, dict) else None
            raise ExchangeFailed(inner_msg or ret_text)

        raise SessionExpired("token 重试后仍失败，请更新 cookie")


# ---------------------------------------------------------------- Gateway

class TaoCoinGateway:
    """领域形状的淘金币通道。生产用默认 HTTP transport，测试注 fake。"""

    def __init__(self, cookie, transport=None):
        self._transport = transport or MtopClient(cookie)

    def fetch_benefits(self):
        """查首页权益列表，返回 HomeSnapshot。业务 code 非 200 抛 ExchangeFailed。"""
        _, data = self._transport.request(API_HOME, {"asac": ASAC_HOME})
        inner = self._unwrap(data)
        benefits = [
            Benefit(
                code=b.get("benefitCode"),
                title=b.get("displayTitle", ""),
                coin_amount=b.get("reduceCoinAmount", 0),
                raw=b,
            )
            for b in (inner.get("benefitList") or [])
        ]
        return HomeSnapshot(
            exchanged_all=bool(inner.get("allRedEnvelopeExchanged")),
            benefits=benefits,
        )

    def exchange(self, benefit_code):
        """按 benefitCode 兑换，返回奖品 dict。失败抛 ExchangeFailed。"""
        _, data = self._transport.request(API_EXCHANGE, {
            "benefitCode": benefit_code,
            "asac": ASAC_EXCHANGE,
        })
        inner = self._unwrap(data)
        if not inner:
            raise ExchangeFailed("兑换返回为空: " + json.dumps(data, ensure_ascii=False)[:300])
        log.info("兑换接口返回: %s", json.dumps(inner, ensure_ascii=False)[:300])
        return inner

    @staticmethod
    def _unwrap(data):
        """网关层 SUCCESS 不代表业务成功：检查业务 code，拆 data 层。"""
        inner = data or {}
        if isinstance(inner, dict) and inner.get("code") not in (None, 200):
            raise ExchangeFailed(inner.get("message") or str(inner)[:200])
        return inner.get("data") if isinstance(inner, dict) else {}
