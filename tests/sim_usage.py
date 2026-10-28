"""LINE push・メールの通数の見張りを手元でまねる（push の上限・メールが送れないとき・警告・日報）。"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("INVOICE_TRN", "")
os.environ.setdefault("MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-chintai.json"))
import simulate as s  # 登録とマイソク10枚まで流れる
import bot, delivery, line, store, usage, texts

notices = []
delivery.send_notice = lambda to, subj, text: notices.append((to, subj, text)) or True
usage.line_quota = lambda: ((store.db().get("meta", "usage_m_" + usage._month()) or {}).get("push", 0), 200)


# 本物の line.push の中身（requests）だけをまねる
class R:
    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


limit_reached = {"on": False}


def fake_push(to, messages):
    r = R(429, '{"message":"You have reached your monthly limit."}') if limit_reached["on"] else R(200)
    usage.on_push(to, messages, r.status_code, r.text)
    s.SENT.append(("push", messages)) if r.status_code == 200 else None
    return r.status_code == 200


line.push = fake_push

print("\n=== push を数える ===")
for i in range(3):
    line.push("Uabc", [line.text(f"お知らせ{i}")])
print(usage.stats()["line_push"])
assert usage.stats()["line_push"]["month"] == 3

print("\n=== 上限で送れない：ためて、次の返信で届ける ===")
limit_reached["on"] = True
bot.notify("LUabc", "plan_payment_failed", {"猶予日": "11月4日（水）"})
u = store.get_user("LUabc")
print("ためた件数:", len(u.get("held_pushes") or []), "運営への知らせ:", [n[1] for n in notices])
assert len(u["held_pushes"]) == 1
assert any("【至急】" in n[1] for n in notices)
s.pb("plan=status")  # 返信と一緒に届く
assert not store.get_user("LUabc").get("held_pushes")

print("\n=== 警告（50%・75%・見込み）は1日1回ずつ ===")
limit_reached["on"] = False
store.db().incr("meta", "usage_m_" + usage._month(), {"push": 160})
notices.clear()
print(usage.check(), usage.check())
print([n[1] for n in notices])
assert sum("75%" in n[1] for n in notices) == 1

print("\n=== メールが送れない：LINEで届けて一言 ===")
delivery.RESEND_API_KEY = "x"
delivery.send_mail = lambda *a, **k: False
store.update_user("LUabc", {"delivery": {**store.get_user("LUabc")["delivery"], "method": "email", "email": "a@example.com"}})
notices.clear()
s.image(s.img)
print([n[1] for n in notices])
assert any("メールが送れず" in n[1] for n in notices)

print("\n=== 日報 ===")
notices.clear()
usage.daily_report()
print(notices[0][1]); print(notices[0][2])
assert notices[0][1] == "【1分資料 日報】"
print("OK")
