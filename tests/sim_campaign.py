"""店舗まとめ割・新人さん応援を手元でまねる（Stripe はまね）。

STORE=local python tests/sim_campaign.py
"""
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-chintai.json"))
os.environ["SHOW"] = os.environ.get("SHOW", "600")
import simulate as s  # 登録まで（LUabc が代表）
import billing, bot, campaign, config, store  # noqa: E402

bot.pool = type("P", (), {"submit": staticmethod(lambda f, *a: f(*a))})()
billing.tax_rate = lambda inclusive=False: "txr_test"
NOW = int(time.time())


class Obj(dict):
    __getattr__ = dict.get

    def to_dict(self):
        return dict(self)


class FakeStripe:
    InvalidRequestError = type("E", (Exception,), {})
    subs = {}
    calls = []

    class checkout:
        class Session:
            @staticmethod
            def create(**kw):
                FakeStripe.calls.append(("checkout", kw))
                return Obj(url="https://checkout.stripe.test/x")

    class Subscription:
        @staticmethod
        def retrieve(sid):
            return Obj(FakeStripe.subs[sid])

        @staticmethod
        def modify(sid, **kw):
            FakeStripe.calls.append(("modify", sid, kw))
            sub = FakeStripe.subs.setdefault(sid, {"id": sid})
            if "items" in kw:
                it = sub["items"]["data"][0]
                for k in ("quantity", "price"):
                    if k in kw["items"][0]:
                        it[k] = kw["items"][0][k] if k == "quantity" else {"id": kw["items"][0][k]}
            if "cancel_at_period_end" in kw:
                sub["cancel_at_period_end"] = kw["cancel_at_period_end"]
            return Obj(sub)

    class Coupon:
        @staticmethod
        def retrieve(cid):
            return Obj(id=cid)


billing._stripe = lambda: FakeStripe


def as_user(line_uid):
    s_ev = s.ev

    def ev(kind, **kw):
        s.n += 1
        e = {"type": kind, "replyToken": f"tok{s.n}", "webhookEventId": f"e{s.n}",
             "source": {"type": "user", "userId": line_uid}}
        e.update(kw)
        bot.handle(e)
        s.show()
    return ev


def text(who, t):
    print(f"\n>>> [{who}] {t}")
    as_user(who)("message", message={"type": "text", "id": f"m{s.n}", "text": t})


def pb(who, d):
    print(f"\n>>> [{who}] (ボタン) {d}")
    as_user(who)("postback", postback={"data": d})


print("\n========== 店舗まとめ割：代表の申し込み ==========")
pb("Uabc", "plan=status")
pb("Uabc", "store=start")
pb("Uabc", "store=apply")
pb("Uabc", "store=plan&p=lite")
text("Uabc", "音羽トレンディ 護国寺店")
text("Uabc", "2")
text("Uabc", "4人")
url = [c for c in FakeStripe.calls if c[0] == "checkout"]
print("（決済ページへ）", end=" ")
from main import pay  # noqa: E402
r = pay("store", t=billing.sign("LUabc"))
ck = FakeStripe.calls[-1][1]
print(r.status_code, ck["line_items"], ck["metadata"])

print("\n========== Stripe：支払い完了 ==========")
md = ck["subscription_data"]["metadata"]
code = md["code"]
FakeStripe.subs["sub_g"] = {"id": "sub_g", "customer": "cus_owner", "status": "active", "metadata": md,
                            "start_date": NOW, "cancel_at_period_end": False,
                            "items": {"data": [{"id": "si_g", "quantity": 4, "price": {"id": config.PRICE_GROUP_LIGHT},
                                                "current_period_start": NOW, "current_period_end": NOW + 30 * 86400}]}}
for uid, mid, kw in billing_notices if (billing_notices := campaign.on_paid(
        {"client_reference_id": "LUabc", "customer": "cus_owner", "subscription": "sub_g", "metadata": md})) else []:
    bot.notify(uid, mid, kw)
s.show()
u = store.get_user("LUabc")
print("代表:", u["plan"], u["plan_source"], u["shop_code"], u["shop_role"], billing.plan_name(u))

print("\n========== メンバー1（新しい方）：コードを送る ==========")
as_user("Um1")("follow");
text("Um1", "ot-s-zzzz")
text("Um1", code.lower())
text("Um1", "山田 花子")
text("Um1", "Hanako Yamada")
text("Um1", "なし")
text("Um1", "090-1111-2222")
pb("Um1", "recv=line")
m1 = store.get_user("LUm1")
print("メンバー1:", m1["plan"], m1["plan_source"], m1["registered"], m1["company"].get("会社名"), m1["company"].get("担当者名"))

print("\n========== メンバー2：個人で契約中の方 ==========")
store.user_by_line("Um2")
store.update_user("LUm2", {"registered": True, "plan": "unlimited", "plan_source": "stripe",
                           "stripe_subscription": "sub_p2", "stripe_customer": "cus_p2",
                           "company": {"会社名": "旧会社", "担当者名": "佐藤 一郎", "名刺済み": True}})
FakeStripe.subs["sub_p2"] = {"id": "sub_p2"}
text("Um2", code)
pb("Um2", f"store=join&code={code}")
m2 = store.get_user("LUm2")
print("メンバー2:", m2["plan"], m2["plan_source"], "個人契約の解約予約:", FakeStripe.subs["sub_p2"].get("cancel_at_period_end"))
print("（個人の契約が終わった）")
billing.sync_subscription({"id": "sub_p2", "customer": "cus_p2", "status": "canceled", "metadata": {"uid": "LUm2"},
                           "items": {"data": [{"id": "si", "price": {"id": config.PRICE_UNLIMITED}}]}})
m2 = store.get_user("LUm2")
print("メンバー2:", m2["plan"], m2["plan_source"], billing.plan_name(m2))

print("\n========== 4人目 → 5人目は満員 ==========")
text("Um3", code)
text("Um4", code)

print("\n========== メンバー管理 ==========")
pb("Um1", "store=members")
pb("Uabc", "plan=status")
pb("Uabc", "store=members")
pb("Uabc", "store=add")
pb("Uabc", "store=add&n=1")
pb("Uabc", "store=reduce")
pb("Uabc", "store=reduce&n=1")
pb("Uabc", "store=reduce")
pb("Uabc", "store=remove")
pb("Uabc", "store=remove&m=LUm1")
pb("Uabc", "store=remove_ok&m=LUm1")
print("メンバー1:", store.get_user("LUm1")["plan"], store.get_user("LUm1")["plan_source"])
pb("Uabc", "store=reduce")

print("\n========== 割引の終わり（14日前のお知らせ・前日の切り替え） ==========")
g = campaign.get_group(code)
end = campaign._jst_date(g["discount_until"])
campaign.today = lambda: end - timedelta(days=14)
for uid, mid, kw in campaign.daily():
    bot.notify(uid, mid, kw)
s.show()
campaign.today = lambda: end
for uid, mid, kw in campaign.daily():
    bot.notify(uid, mid, kw)
print("価格:", FakeStripe.subs["sub_g"]["items"]["data"][0]["price"], "割引終了:", campaign.get_group(code)["discount_ended"])
pb("Uabc", "store=members")

print("\n========== 外れた方の期限が過ぎた ==========")
store.db().update_tx("groups", code, lambda d: d.__setitem__("leaving", [{**x, "until": store.now()} for x in d["leaving"]]))
campaign.daily()
print("メンバー1:", store.get_user("LUm1")["plan"], store.get_user("LUm1")["plan_source"])

print("\n========== 個人のプランは申し込めない ==========")
print(pay("light", t=billing.sign("LUm2")).body.decode()[-200:])

print("\n========== 代表が解約 ==========")
FakeStripe.subs["sub_g"]["cancel_at_period_end"] = True
for uid, mid, kw in campaign.on_sub_event("customer.subscription.updated",
                                          {"data": {"previous_attributes": {"cancel_at_period_end": False}}},
                                          FakeStripe.subs["sub_g"]):
    bot.notify(uid, mid, kw)
s.show()
FakeStripe.subs["sub_g"]["status"] = "canceled"
campaign.on_sub_event("customer.subscription.deleted", {"data": {}}, FakeStripe.subs["sub_g"])
for x in ("LUabc", "LUm2", "LUm3"):
    v = store.get_user(x)
    print(x, v["plan"], v.get("plan_source"), v.get("shop_code"))

print("\n========== 受付期間 ==========")
config.STORE_CAMPAIGN_START = "2026-11-11"
campaign.today = lambda: campaign.date(2027, 2, 11)
print("受付期間:", campaign.store_window(), "3/1は受付中か:", campaign.store_open())
pb("Um1", "store=start")
campaign.today = lambda: campaign.date(2026, 11, 11)
pb("Um1", "store=start")

print("\n========== 新人さん応援 ==========")
for d in ("2027-02-28", "2027-03-01", "2027-04-30", "2027-05-01"):
    y, m, dd = map(int, d.split("-"))
    campaign.today = lambda y=y, m=m, dd=dd: campaign.date(y, m, dd)
    print(d, "期間中:", campaign.newbie_active(), "メンバー1の対象:", campaign.newbie_eligible(store.get_user("LUm1")),
          "メンバー2の対象:", campaign.newbie_eligible(store.get_user("LUm2")))
campaign.today = lambda: campaign.date(2027, 3, 10)
store.user_by_line("Unew")
store.update_user("LUnew", {"registered": True, "company": {"会社名": "新人不動産"}})
print("新しい方の対象:", campaign.newbie_eligible(store.get_user("LUnew")))
pb("Unew", "plan=show")
FakeStripe.calls.clear()
billing.checkout("LUnew", "light")
print("月払い:", FakeStripe.calls[-1][1].get("discounts"), FakeStripe.calls[-1][1]["metadata"])
billing.checkout("LUnew", "light", yearly=True)
print("年払い:", FakeStripe.calls[-1][1].get("discounts"))
bot.notify("LUnew", "shinjin_started", {"半額": "740", "通常料金": "1,480", "更新日": "4月10日"})
s.show()
