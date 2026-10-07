#!/usr/bin/env python3
"""淘金币 Gateway：与淘宝淘金币活动服务端交互的唯一通道。

interface 收窄到领域语言：
    fetch_benefits()      -> HomeSnapshot   查可兑换权益列表
    exchange(code)        -> dict           按 benefitCode 兑换，返回奖品
    sign_in()             -> SignOutcome    签到：完整页面挂载序列（town→已签
                                            短路→home热身→签到→同步）由本动词独占
    query_coin_town()     -> CoinTownState  金币小镇状态：余额 + 今日是否已签
                                            （collect 的 +X 收尾测量用）

implementation 藏住：mtop H5 签名、_m_h5_tk token 刷新、API 名、asac 常量、
data.data 双层拆包、ret 错误分类。MtopClient 是内部 transport（私有 seam），
测试通过 transport 参数注入假 adapter。

异常契约：ExchangeFailed 可级联；SessionExpired / RiskControlBlocked 当天中止。
WeeklyLimitReached 是 ExchangeFailed 的子类：级联语义不变，但「已达周限」单独
成类，供收菜日状态机判定（关键词集中在本模块，见 classify_biz_error）。
收取动词的已签到/无可领返币等业务失败也走 ExchangeFailed，由 collect 层当正常结局。
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

# 每日收取接口（从页面 live JS bundle 逆向 + 2026-10-02/07 两次实测校准）
API_SIGN_COLLECT = "mtop.coingame.collect.reward.pc"
API_SIGN_SYNC = "mtop.taobao.pc.growth.taocoin.pcSign4Sync"
API_COIN_TOWN = "mtop.coingame.town.index.get.pc"
# 下单返金币（receivepostpurchasetaocoin）已砍：orderIds 只来自支付后跳转 URL 的
# bizOrderIds 参数，页面无查询待领订单的接口，自动化它需要交易 API，超出签到类范围

# 从页面 JS（p_gold-index.js）逆向得到的固定参数
ASAC_HOME = "2A24C24PP4OZC3YF9XCDIA"
ASAC_EXCHANGE = "2A24A17A33HG02DHKF2BEX"

# coingame 系接口的载荷不含 params：页面 JS 的 params 是序列化 bug（JSON.stringify
# 了函数引用，实际请求不发送）；多带 params 会让签到被服务端静默吞掉（2026-10-07 实测）

# 签到成功时的金币字段：按序尝试，首个真值生效（已签到时整个 data 为 {}）
REWARD_FIELDS = ("totalCoinReward", "coinAmount", "rewardCoin")

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


class WeeklyLimitReached(ExchangeFailed):
    """业务层「已达周限」：级联仍落到下一档，但单独成类，供状态机判定收菜日。"""


# 周限业务文案：ExchangeFailed 消息里出现该关键词即视为「已达周限」
WEEKLY_LIMIT_KEYWORD = "已达周限"


def classify_biz_error(message):
    """按业务文案分类失败：「已达周限」单独成类，其余维持普通 ExchangeFailed。"""
    if WEEKLY_LIMIT_KEYWORD in (message or ""):
        return WeeklyLimitReached(message)
    return ExchangeFailed(message)


# ---------------------------------------------------------------- 领域对象

@dataclass
class Benefit:
    """一条可兑换权益。match_text 是档位关键词匹配的文本面（标题+面额+单位+金币），
    由本模块在解析时预计算——「哪些字段参与匹配」只在这里有主：
    2026-09-30 曾对整段 JSON 子串匹配，图床 URL 里的 "6000000000…"
    误命中 20元档的 "6000" 关键词，把 10元错兑错记。"""
    code: str
    title: str
    coin_amount: int
    match_text: str


@dataclass
class HomeSnapshot:
    exchanged_all: bool
    benefits: list


@dataclass
class CoinTownState:
    """金币小镇状态：余额与今日是否已签（余额的唯一可靠来源，实测 2026-10-02）。"""
    balance: int
    signed: bool


@dataclass
class SignOutcome:
    """一次 sign_in 的结果。skipped=True 表示今日已签、序列短路（只发了 town 一步）；
    balance_before 是签到前余额（起始 town 查询失败时为 None，+X 退化为 reward 自报）。
    收尾余额查询是 collect 的 +X 测量业务，不在本序列内（ADR 0002）。"""
    skipped: bool
    reward: int
    balance_before: int = None


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
            raise classify_biz_error(inner_msg or ret_text)

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
                # 匹配面只拼语义字段；图床 URL 等无关字段到此为止，不过 seam
                match_text="{} {}{} {}".format(
                    b.get("displayTitle", ""),
                    b.get("displayAmount", ""),
                    b.get("displayAmountUnit", ""),
                    b.get("reduceCoinAmount", ""),
                ),
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

    # -------------------------------------------------------- 每日收取动词

    def sign_in(self):
        """签到：完整页面挂载序列由本方法独占（2026-10-07 实测校准，ADR 0001 修订 3、
        ADR 0002）：town 起始 → 已签短路 → home 热身 → 签到 → 同步。
        载荷与页面请求逐字节一致：页面 JS 的 params 是序列化 bug（实际不发送），
        多带会被服务端静默吞掉——SUCCESS 但空 data、不报错。
        热身与同步是会话铺垫（任何失败都 best-effort，含 SessionExpired——签到步刚
        成功即证明登录态活着，cookie 探针职责由 collect 的收尾 town 查询承担）；
        起始 town 失败仍尝试签到（balance_before=None，+X 退化为 reward 自报），
        唯独其 SessionExpired 抛出（登录态已死，签到必然同样失败）。
        签到步的异常（ExchangeFailed 业务失败 / SessionExpired / RiskControlBlocked）
        原样抛出。"""
        before = None
        try:
            before = self.query_coin_town()
        except SessionExpired:
            raise
        except Exception as e:  # noqa: BLE001 - 起始状态只是参照，失败不致命
            log.warning("签到前查询小镇状态失败，仍尝试签到: %s", e)

        if before is not None and before.signed:
            # 已签跳过（实测已签时 collect.reward.pc 返回空 data）：连热身也不发
            return SignOutcome(skipped=True, reward=0,
                               balance_before=before.balance)

        try:
            self.fetch_benefits()  # 页面热身（复刻挂载顺序），结果不需要
        except Exception as e:  # noqa: BLE001 - 热身只是会话铺垫，失败不致命
            log.warning("签到前首页热身失败，仍尝试签到: %s", e)

        _, data = self._transport.request(API_SIGN_COLLECT, {
            "bizCode": "taoCoin",
            "subBizCode": "coinTown",
            "page": "pc",
        })
        reward = self._extract_reward(data, "签到+收币")

        try:  # 签到后状态同步（模仿页面行为），不带金币收益；任何失败都不
            self._sync_sign_status()  # 吞掉已得的 reward——含 SessionExpired：
        except Exception as e:  # noqa: BLE001 - 签到刚成功，登录态必然活着；
            log.warning("签到状态同步失败（签到已成功）: %s", e)  # 探针职责在收尾 town

        return SignOutcome(skipped=False, reward=reward,
                           balance_before=before.balance if before else None)

    def _sync_sign_status(self):
        """签到后状态同步（模仿页面行为），不带金币收益。失败抛 ExchangeFailed。"""
        _, data = self._transport.request(API_SIGN_SYNC, {})
        self._unwrap(data)
        return 0

    def query_coin_town(self):
        """金币小镇状态：余额 + 今日是否已签。实测（2026-10-02）：首页
        queryTaoCoinHomeV2 不带余额，唯一可靠来源是 town 接口的
        model.userInfo.coinAmount；model.userSign.signed 标记今日已签。"""
        _, data = self._transport.request(API_COIN_TOWN, {
            "bizCode": "taoCoin",
            "subBizCode": "coinTown",
        })
        model = (data or {}).get("model")
        if not isinstance(model, dict):
            raise ExchangeFailed("town 响应缺少 model 层: "
                                 + json.dumps(data, ensure_ascii=False)[:200])
        user = model.get("userInfo") or {}
        sign = model.get("userSign") or {}
        return CoinTownState(balance=int(user.get("coinAmount") or 0),
                             signed=bool(sign.get("signed")))

    def _extract_reward(self, data, label):
        """收取类接口的公共拆包：业务 code 检查 + 按 REWARD_FIELDS 取金币。
        金币字段为空视为业务失败（如当日已签到），resultMsg 透出原因。"""
        inner = self._unwrap(data)
        outer = data if isinstance(data, dict) else {}
        for field in REWARD_FIELDS:
            for layer in (inner, outer):
                value = layer.get(field) if isinstance(layer, dict) else None
                if value:
                    return int(value)
        msg = None
        for layer in (inner, outer):
            if isinstance(layer, dict) and layer.get("resultMsg"):
                msg = layer["resultMsg"]
                break
        raise classify_biz_error("{}: {}".format(label, msg or "未返回金币"))

    @staticmethod
    def _unwrap(data):
        """网关层 SUCCESS 不代表业务成功：检查业务 code，拆 data 层。"""
        inner = data or {}
        if isinstance(inner, dict) and inner.get("code") not in (None, 200):
            raise classify_biz_error(inner.get("message") or str(inner)[:200])
        return inner.get("data") if isinstance(inner, dict) else {}
