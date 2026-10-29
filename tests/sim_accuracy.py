"""精度検証（10/30）からの直し：条件が2通りの図面は概算書を作る前に聞く、FAX番号のない図面は内見FAXのボタンを出さない、
自信のない金額は「要確認」。"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("INVOICE_TRN", "")
os.environ.setdefault("MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-chintai.json"))
import simulate as s  # 登録まで流れる
import bot, config, costs, store
from schema import Result

config.MOCK_RESULT = str(ROOT / "tests" / "fixtures" / "mihon-2jouken.json")
print("\n=== 条件が2通り・FAX番号なしの図面 ===")
s.SENT.clear()
bot.handle({"type": "message", "replyToken": "tx1", "webhookEventId": "ex1", "source": {"type": "user", "userId": "Uabc"},
            "message": {"type": "image", "id": "img-2jouken"}}) if False else None
s.image(s.img)
u = store.get_user("LUabc")
jid = u["last_job"]
job = store.get_job(jid)
# 概算書：賃料は「条件により異なります」
f = Result.model_validate(job["result"]).事実
lines = costs.estimate(f, {})[0]
print([(l.項目, l.金額円, l.メモ[:30]) for l in lines[:3]])
assert lines[0].金額円 is None and "条件により異なります" in lines[0].メモ
# 選ぶメッセージと、内見FAXボタンなし
msg = bot.choice_message(jid, job)
assert msg and len(msg["quickReply"]["items"]) == 2
assert bot.nofax(job) and bot.nofax_drop(job)
print("\n>>> 「事務所として」を選ぶ")
s.pb(f"choice=1&doc={jid}")
job = store.get_job(jid)
f = Result.model_validate(job["result"]).事実
assert f.選んだ条件 == 1 and f.賃料.金額円 == 104500
assert costs.estimate(f, {})[0][0].金額円 == 104500
assert bot.choice_message(jid, job) is None
print("\n>>> この物件で内見FAX（FAX番号なし）")
s.pb(f"naiken=from_last&doc={jid}")
print("OK")
