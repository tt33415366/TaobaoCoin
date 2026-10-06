#!/usr/bin/env python3
"""gateway.py 的测试。运行: python3 test_gateway.py"""

import hashlib
import unittest

from gateway import (
    ExchangeFailed,
    MtopClient,
    TaoCoinGateway,
    WeeklyLimitReached,
    classify_biz_error,
    extract_token,
    mtop_sign,
    update_cookie,
)


def make_cookie(name, value):
    from http.cookiejar import Cookie
    return Cookie(0, name, value, None, False, ".taobao.com", True, True,
                  "/", True, False, None, False, None, None, {}, False)


class TestTokenRefresh(unittest.TestCase):
    def test_refresh_updates_token_and_enc_pair(self):
        # token 和 _m_h5_tk_enc 是配对校验的：只换一个会 FAIL_SYS_TOKEN_ILLEGAL
        client = MtopClient("cookie2=x; _m_h5_tk=old_111; _m_h5_tk_enc=oldenc")
        client.cj.set_cookie(make_cookie("_m_h5_tk", "new_222"))
        client.cj.set_cookie(make_cookie("_m_h5_tk_enc", "newenc"))
        self.assertTrue(client._refresh_token_from_jar())
        self.assertEqual(client._token, "new")
        self.assertIn("_m_h5_tk=new_222", client.cookie_str)
        self.assertIn("_m_h5_tk_enc=newenc", client.cookie_str)
        self.assertNotIn("oldenc", client.cookie_str)

    def test_refresh_appends_when_absent(self):
        client = MtopClient("cookie2=x")
        client.cj.set_cookie(make_cookie("_m_h5_tk", "new_222"))
        client.cj.set_cookie(make_cookie("_m_h5_tk_enc", "newenc"))
        client._refresh_token_from_jar()
        self.assertIn("_m_h5_tk=new_222", client.cookie_str)
        self.assertIn("_m_h5_tk_enc=newenc", client.cookie_str)

    def test_refresh_returns_false_without_token(self):
        client = MtopClient("cookie2=x")
        self.assertFalse(client._refresh_token_from_jar())


class TestUpdateCookie(unittest.TestCase):
    def test_replaces_existing(self):
        self.assertEqual(update_cookie("a=1; b=2", "a", "9"), "a=9; b=2")

    def test_appends_when_absent(self):
        self.assertEqual(update_cookie("a=1", "b", "2"), "a=1; b=2")


class FakeTransport:
    """MtopClient 位置的假 adapter：按 api 返回脚本化响应，记录调用。"""

    def __init__(self, script):
        self.script = script  # {api: (ret, data) 或 异常}
        self.calls = []

    def request(self, api, data_dict):
        self.calls.append((api, data_dict))
        result = self.script[api]
        if isinstance(result, Exception):
            raise result
        return result


HOME_OK = ("SUCCESS", {"code": 200, "data": {
    "allRedEnvelopeExchanged": False,
    "benefitList": [
        {"benefitCode": "code20", "displayTitle": "20元红包", "reduceCoinAmount": 6000},
        {"benefitCode": "code10", "displayTitle": "10元红包", "reduceCoinAmount": 3000},
    ],
}})


class TestWeeklyLimitClassification(unittest.TestCase):
    """prefactor：「已达周限」从裸 ExchangeFailed 字符串中分类出来。
    级联仍当 ExchangeFailed 捕获（子类），状态机据类型判定收菜日。"""

    def test_weekly_limit_message_raises_dedicated_subclass(self):
        exc = classify_biz_error("已达周限，请下周再来")
        self.assertIsInstance(exc, WeeklyLimitReached)
        self.assertIsInstance(exc, ExchangeFailed)

    def test_other_biz_message_stays_plain_exchange_failed(self):
        exc = classify_biz_error("权益已变更，请刷新后重试")
        self.assertNotIsInstance(exc, WeeklyLimitReached)
        self.assertIsInstance(exc, ExchangeFailed)

    def test_exchange_path_raises_weekly_limit(self):
        gw = TaoCoinGateway("c", transport=FakeTransport({
            "mtop.taobao.pc.growth.taocoin.exchangeBenefit":
                ("SUCCESS", {"code": -1, "message": "已达周限"}),
        }))
        with self.assertRaises(WeeklyLimitReached):
            gw.exchange("code20")


class TestToken(unittest.TestCase):
    def test_extract_token(self):
        cookie = "sgcookie=x; _m_h5_tk=abc123def_1717000000000; _m_h5_tk_enc=zzz"
        self.assertEqual(extract_token(cookie), "abc123def")

    def test_extract_token_missing(self):
        self.assertIsNone(extract_token("cookie2=abc; other=x"))


class TestSign(unittest.TestCase):
    def test_sign_known_vector(self):
        expected = hashlib.md5(b'tok&1000&12574478&{"a":1}').hexdigest()
        self.assertEqual(mtop_sign("tok", "1000", "12574478", '{"a":1}'), expected)


class TestFetchBenefits(unittest.TestCase):
    def test_unwraps_double_data_layer(self):
        # data.data.benefitList 的拆包收进 gateway，调用方只见领域对象
        gw = TaoCoinGateway("cookie2=x", transport=FakeTransport({
            "mtop.taobao.pc.growth.taocoin.queryTaoCoinHomeV2": HOME_OK,
        }))
        snap = gw.fetch_benefits()
        self.assertFalse(snap.exchanged_all)
        self.assertEqual(len(snap.benefits), 2)
        self.assertEqual(snap.benefits[0].code, "code20")
        self.assertEqual(snap.benefits[0].title, "20元红包")
        self.assertEqual(snap.benefits[0].coin_amount, 6000)
        # raw 保留完整字典供关键词匹配
        self.assertIn("benefitCode", snap.benefits[0].raw)

    def test_exchanged_all_flag(self):
        ret, data = HOME_OK
        data2 = {"code": 200, "data": {"allRedEnvelopeExchanged": True, "benefitList": []}}
        gw = TaoCoinGateway("c", transport=FakeTransport({
            "mtop.taobao.pc.growth.taocoin.queryTaoCoinHomeV2": (ret, data2),
        }))
        self.assertTrue(gw.fetch_benefits().exchanged_all)

    def test_business_error_code_raises_exchange_failed(self):
        gw = TaoCoinGateway("c", transport=FakeTransport({
            "mtop.taobao.pc.growth.taocoin.queryTaoCoinHomeV2":
                ("SUCCESS", {"code": -1, "message": "权益已变更"}),
        }))
        with self.assertRaises(ExchangeFailed) as cm:
            gw.fetch_benefits()
        self.assertIn("权益已变更", str(cm.exception))

    def test_sends_asac_param(self):
        t = FakeTransport({
            "mtop.taobao.pc.growth.taocoin.queryTaoCoinHomeV2": HOME_OK,
        })
        TaoCoinGateway("c", transport=t).fetch_benefits()
        self.assertEqual(t.calls[0][1], {"asac": "2A24C24PP4OZC3YF9XCDIA"})


class TestExchange(unittest.TestCase):
    API = "mtop.taobao.pc.growth.taocoin.exchangeBenefit"

    def test_success_returns_award(self):
        gw = TaoCoinGateway("c", transport=FakeTransport({
            self.API: ("SUCCESS", {"code": 200, "data": {"awardTitle": "10元红包"}}),
        }))
        award = gw.exchange("code10")
        self.assertEqual(award, {"awardTitle": "10元红包"})

    def test_empty_inner_data_raises(self):
        gw = TaoCoinGateway("c", transport=FakeTransport({
            self.API: ("SUCCESS", {"code": 200, "data": None}),
        }))
        with self.assertRaises(ExchangeFailed):
            gw.exchange("code10")

    def test_business_failure_raises_with_message(self):
        gw = TaoCoinGateway("c", transport=FakeTransport({
            self.API: ("SUCCESS", {"code": -1, "message": "金币不足"}),
        }))
        with self.assertRaises(ExchangeFailed) as cm:
            gw.exchange("code10")
        self.assertIn("金币不足", str(cm.exception))

    def test_sends_benefit_code_and_asac(self):
        t = FakeTransport({
            self.API: ("SUCCESS", {"code": 200, "data": {"ok": 1}}),
        })
        TaoCoinGateway("c", transport=t).exchange("code10")
        _, data = t.calls[0]
        self.assertEqual(data["benefitCode"], "code10")
        self.assertEqual(data["asac"], "2A24A17A33HG02DHKF2BEX")

    def test_transport_exceptions_propagate(self):
        from gateway import SessionExpired
        gw = TaoCoinGateway("c", transport=FakeTransport({
            self.API: SessionExpired("登录失效"),
        }))
        with self.assertRaises(SessionExpired):
            gw.exchange("code10")


class TestCollectVerbs(unittest.TestCase):
    """收取动词：签名+收币、签到同步、金币小镇状态。payload 形状见 gateway 常量。"""
    SIGN_API = "mtop.coingame.collect.reward.pc"
    SYNC_API = "mtop.taobao.pc.growth.taocoin.pcSign4Sync"
    TOWN_API = "mtop.coingame.town.index.get.pc"

    def test_sign_collect_sends_page_payload(self):
        t = FakeTransport({
            self.SIGN_API: ("SUCCESS", {"code": 200, "data": {"totalCoinReward": 5}}),
        })
        gw = TaoCoinGateway("c", transport=t)
        self.assertEqual(gw.collect_sign_reward(), 5)
        _, data = t.calls[0]
        # 与页面请求逐字节一致（2026-10-07 实测）：页面 JS 的 params 是序列化 bug
        # 实际不发送；多带 params 会被服务端静默吞掉（SUCCESS 但空 data）
        self.assertEqual(data, {
            "bizCode": "taoCoin",
            "subBizCode": "coinTown",
            "page": "pc",
        })

    def test_sign_collect_reward_at_outer_layer(self):
        # coingame 系接口可能不套双层 data：外层的 totalCoinReward 也要认
        gw = TaoCoinGateway("c", transport=FakeTransport({
            self.SIGN_API: ("SUCCESS", {"code": 200, "totalCoinReward": 8}),
        }))
        self.assertEqual(gw.collect_sign_reward(), 8)

    def test_sign_collect_failure_uses_result_msg(self):
        # 当日已签到等场景：totalCoinReward 为空，resultMsg 透出原因
        gw = TaoCoinGateway("c", transport=FakeTransport({
            self.SIGN_API: ("SUCCESS", {"code": 200,
                                        "data": {"resultMsg": "今日已签到"}}),
        }))
        with self.assertRaises(ExchangeFailed) as cm:
            gw.collect_sign_reward()
        self.assertIn("今日已签到", str(cm.exception))

    def test_sign_collect_business_error_code(self):
        gw = TaoCoinGateway("c", transport=FakeTransport({
            self.SIGN_API: ("SUCCESS", {"code": -1, "message": "活动太火爆"}),
        }))
        with self.assertRaises(ExchangeFailed) as cm:
            gw.collect_sign_reward()
        self.assertIn("活动太火爆", str(cm.exception))

    def test_sync_sign_status_sends_empty_data(self):
        t = FakeTransport({
            self.SYNC_API: ("SUCCESS", {"code": 200, "data": {"ok": True}}),
        })
        gw = TaoCoinGateway("c", transport=t)
        self.assertEqual(gw.sync_sign_status(), 0)  # 同步只模仿页面，不带收益
        self.assertEqual(t.calls[0][1], {})

    def test_query_coin_town_parses_balance_and_signed(self):
        # 实测信封（2026-10-02）：model.userInfo.coinAmount / model.userSign.signed
        t = FakeTransport({
            self.TOWN_API: ("SUCCESS", {"model": {
                "userInfo": {"coinAmount": 154058},
                "userSign": {"signed": True},
            }, "resultCode": "SUCCESS"}),
        })
        gw = TaoCoinGateway("c", transport=t)
        state = gw.query_coin_town()
        self.assertEqual(state.balance, 154058)
        self.assertTrue(state.signed)
        self.assertEqual(t.calls[0][1], {
            "bizCode": "taoCoin",
            "subBizCode": "coinTown",
        })

    def test_query_coin_town_not_signed(self):
        gw = TaoCoinGateway("c", transport=FakeTransport({
            self.TOWN_API: ("SUCCESS", {"model": {
                "userInfo": {"coinAmount": 100},
                "userSign": {"signed": False},
            }}),
        }))
        state = gw.query_coin_town()
        self.assertEqual(state.balance, 100)
        self.assertFalse(state.signed)

    def test_query_coin_town_missing_model_raises(self):
        gw = TaoCoinGateway("c", transport=FakeTransport({
            self.TOWN_API: ("SUCCESS", {"unexpected": True}),
        }))
        with self.assertRaises(ExchangeFailed):
            gw.query_coin_town()


if __name__ == "__main__":
    unittest.main(verbosity=2)
