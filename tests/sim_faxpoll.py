"""faximo の送信結果を問い合わせる流れ（届いた・話し中で再送・失敗で回数を戻す・受け取り用FAXの失敗）を手元でまねる。"""
import os, sys
from datetime import timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("INVOICE_TRN", "")
os.environ.setdefault("MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-chintai.json"))
import simulate as s
import bot, delivery, faxopt, line, naiken, store

pushed = []
line.push = lambda to, msgs: pushed.append(msgs) or True
delivery.fax_ready = lambda: True
seq = {"n": 0}
sent_pdfs = []
def fake_send(number, pdf, sender):
    seq["n"] += 1
    sent_pdfs.append(pdf)
    return f"FX{seq['n']}"
delivery._fax_api_send = fake_send
results = {}
delivery._fax_api_status = lambda pid: results.get(pid, ("sending", ""))

def due():
    for pid, _ in store.db().all("fax_sends"):
        store.db().set("fax_sends", pid, {"next_check": store.now() - timedelta(seconds=1)}, merge=True)

uid = "LUabc"
store.update_user(uid, {"plan": "light", "company": {**store.get_user(uid)["company"], "FAX": "03-9999-0000"}})
d = {"to": {"company": "テスト管理", "fax": "03-1234-5678"}, "prop": {"name": "テストハイツ", "room": "101"},
     "wishes": [{"date": "11/12", "time": "14:00"}]}
fid, rec = naiken.save_preview(uid, d, store.get_user(uid)["company"])
print("送信:", naiken.send(uid, fid))
# faximo へ渡すのは送信用（黒）のPDF。スマホで見る控え（青）とは別物（10/31 売買版からの引き継ぎ）
assert sent_pdfs and sent_pdfs[0] == store.db().get_file(f"faxes/{fid}/naiken_fax.pdf"), "送信用PDFが使われていない"
assert sent_pdfs[0] != store.db().get_file(f"faxes/{fid}/naiken.pdf")
m0 = naiken.remaining(store.get_user(uid))[0]

print("\n=== 話し中で再送（1回だけ知らせる） ===")
results["FX1"] = ("retrying", "busy"); due(); print(delivery.poll_faxes()); due(); print(delivery.poll_faxes())
assert len(pushed) == 1, pushed
print(pushed[-1][0].get("text", "")[:40])

print("\n=== 失敗：回数を戻す ===")
results["FX1"] = ("failed", "busy"); due(); delivery.poll_faxes()
print(pushed[-1][0].get("text") or pushed[-1][0].get("altText"))
assert naiken.remaining(store.get_user(uid))[0] == m0 + 1
assert store.db().get("faxes", fid)["status"] == "failed"

print("\n=== もう一度送る → 届いた ===")
print("送信:", naiken.send(uid, fid))
results["FX2"] = ("sent", ""); due(); delivery.poll_faxes()
t = pushed[-1][0].get("text") or pushed[-1][0].get("altText")
print(t)
assert "送信しました" in t and store.db().get("faxes", fid)["status"] == "sent"

print("\n=== 受け取り用FAXの失敗：回数券を戻す ===")
faxopt.add_pack(uid)
store.update_user(uid, {"delivery": {**(store.get_user(uid).get("delivery") or {}), "fax": "03-9999-0000"}})
u = store.get_user(uid)
left0 = faxopt.left(u)
jid = sorted(store.db().all("jobs"), key=lambda x: str(x[1].get("created_at", "")))[-1][0]
n = faxopt.send_job(uid, u, jid, store.db().get("jobs", jid), bot.company_for_render(uid, u))
print("のこり:", n)
assert n is not None
results["FX3"] = ("failed", "busy"); due(); delivery.poll_faxes()
print(pushed[-1][0].get("text", "")[:60])
assert faxopt.left(store.get_user(uid)) == left0
print("OK")
