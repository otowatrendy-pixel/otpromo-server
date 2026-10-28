"""受け取り用FAX（回数券 10回1,000円）を手元でまねる（FAX送信はまね）。"""
import os, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("INVOICE_TRN", "")  # 手元では Stripe に登録番号を作りに行かない
os.environ.setdefault("MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-chintai.json"))
import simulate as s  # 登録とマイソク10枚まで流れる
import bot, delivery, faxopt, store
bot.pool = type("P", (), {"submit": staticmethod(lambda f, *a: f(*a))})()  # その場で動かす
sent = []
delivery.fax_ready = lambda: True
delivery.send_fax = lambda num, pdf: sent.append((num, len(pdf), pdf[:4])) or True
u = store.get_user("LUabc")
store.update_user("LUabc", {"plan": "light", "period_start": store.now(), "faxrecv_packs": [],
                            "delivery": {**u["delivery"], "method": "fax", "fax": "03-1234-9999"}})
def press(how="send"):
    s.pb(f"recvfax={how}&doc={store.get_user('LUabc')['last_job']}")


print("\n=== 資料ができたら、FAXの前に確認（まだ送らない） ===")
s.image(s.img)
assert len(sent) == 0, "確認の前にFAXを送っている"
print("\n=== 10分たっても押されない：1回だけお知らせ ===")
pushed = []
_push = bot.line.push
bot.line.push = lambda to, msgs: pushed.append(msgs) or True
store.update_user("LUabc", {"recvfax_remind_at": store.now()})
print("お知らせ:", bot.remind_recvfax(), "2回目:", bot.remind_recvfax())
assert len(pushed) == 1
bot.line.push = _push
print("\n=== 回数券なしで「このままFAXする」：購入案内 ===")
press()
print("送ったFAX:", len(sent), "待ち:", store.get_user("LUabc").get("recvfax_wait"))
assert len(sent) == 0 and store.get_user("LUabc").get("recvfax_wait")
print("\n=== 回数券を買った（Stripeの通知） ===")
faxopt.add_pack("LUabc")
bot.notify("LUabc", "recvfax_done", {"有効期限": "1月27日（水）", "受取FAX残": faxopt.left(store.get_user("LUabc"))})
time.sleep(3)
print("送ったFAX:", len(sent), "のこり:", faxopt.left(store.get_user("LUabc")))
store.update_user("LUabc", {"faxrecv_packs": [{**store.get_user("LUabc")["faxrecv_packs"][0], "used": 7}]})
print("\n=== のこり2回→1回：少ない案内 ===")
s.image(s.img)
press()
print("送ったFAX:", len(sent), "のこり:", faxopt.left(store.get_user("LUabc")))
print("\n=== メニュー：のこり件数・プラン ===")
s.pb("plan=status")
print("\n=== FAXにも送る（のこり0にしてから） ===")
store.update_user("LUabc", {"faxrecv_packs": [{**store.get_user("LUabc")["faxrecv_packs"][0], "used": 10}]})
s.pb("send=fax")
s.pb("recvfax=show")
s.pb("recv=line")
print("受け取り方:", store.get_user("LUabc")["delivery"]["method"])

print("\n=== お試し中：最初の3件まで無料、4件目は案内 ===")
store.update_user("LUabc", {"plan": "free", "trial_fax_used": 0, "faxrecv_packs": [], "free_limit": 100,
                            "delivery": {**store.get_user("LUabc")["delivery"], "method": "fax"}})
n0 = len(sent)
for i in range(4):
    s.image(s.img)
    press()
print("お試しで送ったFAX:", len(sent) - n0, "使った数:", store.get_user("LUabc").get("trial_fax_used"))
assert len(sent) - n0 == 3
print("\n=== LINEだけで受け取る ===")
s.image(s.img)
press("skip")
assert len(sent) - n0 == 3
