"""使い始めのお知らせ・解約理由・週報・費用の上限・FAXの送信停止リストを手元でまねる。"""
import os, sys
from datetime import timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("INVOICE_TRN", "")
os.environ.setdefault("MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-chintai.json"))
import simulate as s  # 登録と募集図面10枚まで流れる
import bot, delivery, growth, line, naiken, store, usage

notices = []
delivery.send_notice = lambda to, subj, text: notices.append((to, subj, text)) or True
pushed = []
line.push = lambda to, msgs: pushed.append((to, msgs)) or True
growth.in_window = lambda t=None: True

print("\n=== 1 登録して1件も送っていない方：翌日と3日後 ===")
store.db().set("users", "LUnew", {**store.new_user(line_uid="Unew"), "registered": True,
                                  "registered_at": store.now() - timedelta(days=4)})
growth.on_registered("LUnew")
store.update_user("LUnew", {"nudge_at": store.now() - timedelta(minutes=1)})
print("1回目:", growth.run_due())
u = store.get_user("LUnew")
print("次:", u.get("nudge_step"), u.get("nudge_at"))
store.update_user("LUnew", {"nudge_at": store.now() - timedelta(minutes=1)})
print("2回目:", growth.run_due(), "3回目:", growth.run_due())
assert len(pushed) == 2 and not store.get_user("LUnew").get("nudge_at")

print("\n=== 2 無料を使い切った方：3日後に1回 ===")
pushed.clear()
growth.on_free_used_up("LUnew", store.get_user("LUnew"))
store.update_user("LUnew", {"upsell_at": store.now() - timedelta(minutes=1)})
print("案内:", growth.run_due(), "もう一度:", growth.on_free_used_up("LUnew", store.get_user("LUnew")), growth.run_due())
assert len(pushed) == 1

print("\n=== 3 解約の理由 ===")
pushed.clear()
bot.notify("LUabc", "plan_canceled", {})
print("届いたメッセージ数:", len(pushed[0][1]))
s.pb("cancel_reason=price")
assert store.get_user("LUabc")["cancel_reason"] == "price"

print("\n=== 4・6 日報（使っていない有料の方・読み取れなかった件数） ===")
store.update_user("LUabc", {"plan": "light", "last_doc_at": store.now() - timedelta(days=20)})
store.update_job("dummy", {"status": "failed", "error": "blurry"})
notices.clear()
usage.daily_report()
print(notices[0][2])
assert "使っていない方：1人" in notices[0][2]

print("\n=== 5 週報 ===")
notices.clear()
print(growth.weekly_report())
print(notices[0][1]); print(notices[0][2])

print("\n=== 8 費用の上限と1日の上限 ===")
store.db().incr("meta", "usage_m_" + usage._month(), {"ai_yen": 50001})
usage._ai["at"] = 0
s.image(s.img)
store.db().incr("meta", "usage_m_" + usage._month(), {"ai_yen": -50001})
usage._ai["at"] = 0
store.update_user("LUabc", {"day_key": usage._day(), "day_docs": 60})
s.image(s.img)
store.update_user("LUabc", {"day_docs": 0})

print("\n=== 9・10 FAXの送信元と送信停止リスト ===")
print(delivery.fax_sender("テスト不動産株式会社"))
naiken.optout_add("03-1111-2222", "テスト")
print(naiken.opted_out("0311112222"), naiken.optout_list())
assert naiken.opted_out("03(1111)2222")
naiken.optout_remove("03-1111-2222")
assert not naiken.opted_out("03-1111-2222")
print("OK")
