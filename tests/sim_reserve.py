"""内見FAXの送信予約（予約・決まりに合わない日時・日時の変更・取り消し・時刻になったら送る）を手元でまねる。"""
import os, sys
from datetime import datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("INVOICE_TRN", "")
os.environ.setdefault("MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-chintai.json"))
import simulate as s
import delivery, line, naiken, store

pushed = []
line.push = lambda to, msgs: pushed.append(msgs) or True
delivery.fax_ready = lambda: True
sent = []
delivery._fax_api_send = lambda number, pdf, sender: sent.append(number) or f"FX{len(sent)}"

uid = "LUabc"
store.update_user(uid, {"plan": "light"})
JST = store.JST
now = datetime.now(JST)
# 3日後の14時に内見、送信は2日後の10時
visit = (now + timedelta(days=3)).replace(hour=14, minute=0, second=0, microsecond=0)
at = (now + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
d = {"to": {"company": "テスト管理", "fax": "03-1234-5678"}, "prop": {"name": "テストハイツ", "room": "101"},
     "wishes": [{"date": visit.strftime("%Y-%m-%d"), "time": "14:00"}]}
fid, rec = naiken.save_preview(uid, d, store.get_user(uid)["company"])
m0 = naiken.remaining(store.get_user(uid))[0]

print("=== 決まりに合わない日時 ===")
cases = {"too_soon": now + timedelta(minutes=5), "too_far": now + timedelta(days=8),
         "night": (now + timedelta(days=1)).replace(hour=21, minute=0),
         "after_visit": visit - timedelta(minutes=30)}
for want, t in cases.items():
    got = naiken.reserve(uid, fid, t)
    print(want, got, naiken.error_text(got, d))
    assert got == want, (want, got)
assert naiken.reserve(uid, fid, (now + timedelta(days=1)).replace(hour=20, minute=0)) == "scheduled"  # 20:00ちょうどは可
assert naiken.unreserve(uid, fid) == "canceled"
assert naiken.remaining(store.get_user(uid))[0] == m0

print("\n=== LINEで予約（日時選択の postback） ===")
store.update_user(uid, {"nk_fid": fid, "nk": d, "nk_step": "confirm"})
s.pb("naiken=reserve&dt=" + at.strftime("%Y-%m-%dT%H:%M"))
r = store.db().get("faxes", fid)
assert r["status"] == "scheduled", r["status"]
assert naiken.remaining(store.get_user(uid))[0] == m0 - 1  # 予約した時点で1回分
print("\n=== 「予約」と送る ===")
s.text("予約")
print("\n=== 今すぐ送るを押しても二重にならない ===")
s.pb("naiken=send")
print("\n=== 日時の変更 ===")
at2 = at + timedelta(hours=1)
s.pb(f"naiken=resched:{fid}&dt=" + at2.strftime("%Y-%m-%dT%H:%M"))
assert store.db().get("faxes", fid)["send_at"].astimezone(JST).hour == at2.hour
print("\n=== 時刻前は送らない ===")
assert naiken.run_reserved() == 0 and not sent
print("\n=== 時刻になったら送る（1回だけ） ===")
store.db().set("faxes", fid, {"send_at": store.now() - timedelta(minutes=1)}, merge=True)
n = naiken.run_reserved(); print(n, sent, store.db().get("faxes", fid)["status"]); assert n == 1 and len(sent) == 1
assert naiken.run_reserved() == 0 and len(sent) == 1
assert store.db().get("faxes", fid)["status"] == "sending"
assert naiken.remaining(store.get_user(uid))[0] == m0 - 1  # 二重に数えない
print("\n=== 送信の5分前を過ぎたら取り消せない ===")
fid2, _ = naiken.save_preview(uid, d, store.get_user(uid)["company"])
assert naiken.reserve(uid, fid2, at) == "scheduled"
store.db().set("faxes", fid2, {"send_at": store.now() + timedelta(minutes=3)}, merge=True)
assert naiken.unreserve(uid, fid2) == "too_late"
print("\n=== 予約の後に送信停止になった番号は送らず回数を戻す ===")
naiken.optout_add("03-1234-5678", "テスト")
m1 = naiken.remaining(store.get_user(uid))[0]
store.db().set("faxes", fid2, {"send_at": store.now() - timedelta(minutes=1)}, merge=True)
naiken.run_reserved()
assert store.db().get("faxes", fid2)["status"] == "optout" and len(sent) == 1
assert naiken.remaining(store.get_user(uid))[0] == m1 + 1
print(pushed[-1][0]["text"])
print("\n=== 確認カードに「日時を指定して送る」 ===")
import naiken_bot, texts
items = naiken_bot.with_picker(texts.build("naiken_confirm", text="x"), "naiken=reserve")["quickReply"]["items"]
print([(i["action"]["type"], i["action"]["label"]) for i in items], items[1]["action"]["min"], items[1]["action"]["max"])
assert items[1]["action"]["type"] == "datetimepicker" and items[0]["action"]["data"] == "naiken=send"
print("OK")
