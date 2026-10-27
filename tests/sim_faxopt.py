"""受け取り用FAX（回数券 10回1,000円）を手元でまねる（FAX送信はまね）。"""
import os, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STORE", "local")
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
print("\n=== 回数券なし：LINEで届けて購入案内 ===")
s.image(s.img)
print("送ったFAX:", len(sent), "待ち:", store.get_user("LUabc").get("recvfax_wait"))
print("\n=== 回数券を買った（Stripeの通知） ===")
faxopt.add_pack("LUabc")
bot.notify("LUabc", "recvfax_done", {"有効期限": "1月27日（水）", "受取FAX残": faxopt.left(store.get_user("LUabc"))})
time.sleep(3)
print("送ったFAX:", len(sent), "のこり:", faxopt.left(store.get_user("LUabc")))
store.update_user("LUabc", {"faxrecv_packs": [{**store.get_user("LUabc")["faxrecv_packs"][0], "used": 7}]})
print("\n=== のこり2回→1回：少ない案内 ===")
s.image(s.img)
print("送ったFAX:", len(sent), "のこり:", faxopt.left(store.get_user("LUabc")))
print("\n=== メニュー：のこり件数・プラン ===")
s.pb("plan=status")
print("\n=== FAXにも送る（のこり0にしてから） ===")
store.update_user("LUabc", {"faxrecv_packs": [{**store.get_user("LUabc")["faxrecv_packs"][0], "used": 10}]})
s.pb("send=fax")
s.pb("recvfax=show")
s.pb("recv=line")
print("受け取り方:", store.get_user("LUabc")["delivery"]["method"])
