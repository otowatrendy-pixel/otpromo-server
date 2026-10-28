"""紹介特典を手元でまねる（Stripe はまね）。STORE=local python tests/sim_referral.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
exec(open(Path(__file__).resolve().parent / "sim_campaign.py", encoding="utf-8").read().split('print("\\n========== 店舗まとめ割：代表の申し込み')[0])
import referral  # noqa: E402

FakeStripe.PaymentMethod = type("PM", (), {"list": staticmethod(lambda customer, **k: Obj(data=[
    {"card": {"fingerprint": {"cus_a": "fpA", "cus_b": "fpB", "cus_c": "fpA"}.get(customer)}}]))})
FakeStripe.Coupon.create = staticmethod(lambda **kw: Obj(kw))
FakeStripe.Subscription.delete_discount = staticmethod(lambda sid: FakeStripe.calls.append(("delete_discount", sid)))

# 紹介した方 A（LINE・ライト月払い契約中）
store.update_user("LUabc", {"plan": "light", "plan_source": "stripe", "stripe_customer": "cus_a",
                            "stripe_subscription": "sub_a", "billing_interval": "month",
                            "period_end": store.now() + timedelta(days=20), "paid_before": True})
FakeStripe.subs["sub_a"] = {"id": "sub_a"}
print("\n========== 紹介した方：メニュー ==========")
pb("Uabc", "referral=menu")
code = store.get_user("LUabc")["referral_code"]

print("\n========== 紹介された方 B：コードを送る ==========")
as_user("Ub")("follow")
text("Ub", "OT-ZZZZZ")
text("Ub", code.lower())
text("Ub", code)
text("Uabc", code)
pb("Ub", "plan=show")
FakeStripe.calls.clear()
billing.checkout("LUb", "light")
print("Bの月払い:", FakeStripe.calls[-1][1].get("discounts"), FakeStripe.calls[-1][1]["metadata"])
billing.checkout("LUb", "unlimited", yearly=True)
print("Bの使い放題年払い:", FakeStripe.calls[-1][1].get("discounts"))
campaign.today = lambda: campaign.date(2027, 3, 10)
billing.checkout("LUb", "light")
print("新人さん応援の期間中（月払い）:", FakeStripe.calls[-1][1].get("discounts"))
billing.checkout("LUb", "light", yearly=True)
print("新人さん応援の期間中（年払い）:", FakeStripe.calls[-1][1].get("discounts"))
campaign.today = lambda: campaign.date(2026, 11, 20)

print("\n========== B が支払った ==========")
store.update_user("LUb", {"plan": "light", "plan_source": "stripe", "stripe_customer": "cus_b", "paid_before": True,
                          "stripe_subscription": "sub_b", "company": {"会社名": "紹介先不動産"}})
referral.on_checkout("LUb", {"customer": "cus_b"})
print("記録:", store.db().get("referrals", "LUb")["status"])
for n in referral.on_invoice_paid("LUb", {"amount_paid": 1036, "discounts": []}):
    bot.notify(*n)
s.show()
print("Aの契約:", [c for c in FakeStripe.calls if c[0] == "modify"][-1], "待ち:", store.get_user("LUabc").get("ref_queue"))

print("\n========== 2人目 C（同じカード）==========")
store.user_by_line("Uc")
text("Uc", code)
store.update_user("LUc", {"plan": "light", "plan_source": "stripe", "stripe_customer": "cus_c", "paid_before": True})
referral.on_checkout("LUc", {"customer": "cus_c"})
print("記録:", store.db().get("referrals", "LUc")["status"], referral.on_invoice_paid("LUc", {"amount_paid": 1036}))

print("\n========== 3人目 D：Aはすでに割引が付いているので次の請求へ ==========")
store.user_by_line("Ud")
text("Ud", code)
store.update_user("LUd", {"plan": "light", "plan_source": "stripe", "stripe_customer": "cus_d", "paid_before": True})
FakeStripe.PaymentMethod.list = staticmethod(lambda customer, **k: Obj(data=[]))
referral.on_checkout("LUd", {"customer": "cus_d"})
for n in referral.on_invoice_paid("LUd", {"amount_paid": 1036}):
    bot.notify(*n)
s.show()
print("Aの待ち:", len(store.get_user("LUabc")["ref_queue"]))
print("\n========== A の請求で割引を使った → 次を付ける ==========")
for n in referral.on_invoice_paid("LUabc", {"amount_paid": 1036, "discounts": [{"coupon": {"id": "ref_light_m"}}]}):
    bot.notify(*n)
s.show()
print("Aの待ち:", len(store.get_user("LUabc")["ref_queue"]), "付いている:", store.get_user("LUabc").get("ref_attached"))
print("\n========== D に返金 → Aの割引を外す ==========")
referral.on_refund("cus_d")
print("記録:", store.db().get("referrals", "LUd")["status"], "付いている:", store.get_user("LUabc").get("ref_attached"),
      [c for c in FakeStripe.calls if c[0] == "delete_discount"])

print("\n========== アプリ版 ==========")
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
cl = TestClient(main.app)
h = {"Authorization": "Bearer dev:app1"}
print(cl.get("/v1/me", headers=h).json()["referral"])
print(cl.post("/v1/referral", json={"code": "OT-ZZZZZ"}, headers=h).status_code)
print(cl.post("/v1/referral", json={"code": code}, headers=h).json())
uid, _ = store.user_by_firebase("app1")
store.update_user(uid, {"plan": "light", "plan_source": "google_play", "paid_before": True})
for n in referral.on_play_paid(uid):
    print("お知らせ:", n)
print("アプリの方の追加FAX:", store.get_user(uid).get("fax_packs"))
h2 = {"Authorization": "Bearer dev:app2"}
cl.get("/v1/me", headers=h2)
c2 = store.user_by_firebase("app2")[1]
print("自分のコード:", cl.post("/v1/referral", json={"code": store.get_user(store.user_by_firebase("app2")[0])["referral_code"]}, headers=h2).status_code)

print("\n=== 保留（月5件超）を運営が解く ===")
import referral as _ref
_rec = {"referrer": "LUabc", "code": "OT-TEST1", "status": "hold", "established_at": store.now()}
store.db().set("referrals", "Uheld1", _rec)
store.db().set("referrals", "Uheld2", {**_rec, "code": "OT-TEST2"})
print("保留:", [h["referred"] for h in _ref.held()])
print("解く:", _ref.decide("Uheld1", True), "無効:", _ref.decide("Uheld2", False))
assert store.db().get("referrals", "Uheld1")["status"] == "established"
assert store.db().get("referrals", "Uheld2")["status"] == "void"
assert _ref.decide("Uheld1", True) is None
