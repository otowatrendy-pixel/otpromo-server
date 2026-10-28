"""内見FAXの会話を手元でまねる（日時の読み取りは本物のAIを使う）。"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("INVOICE_TRN", "")  # 手元では Stripe に登録番号を作りに行かない
os.environ.setdefault("MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-chintai.json"))
import simulate as s  # 登録とマイソク10枚まで流れる
import store
store.update_user("LUabc", {"free_limit": 100})
s.image(s.img)
s.image(s.img)  # 2物件続けて送り、1枚目の資料のボタンを押す
first = store.get_user("LUabc")["recent_jobs"][1]["id"]
print("\n=== 1枚目の資料のボタン ===", first)
s.pb(f"naiken=from_last&doc={first}")
assert store.get_user("LUabc")["nk"]["job"] == first if "job" in store.get_user("LUabc")["nk"] else True
s.text("明日の14時と、土曜の午前 管理はひかり不動産管理 03-0000-1111")
s.text("山田 花子")
s.pb("card_roman=none")
s.text("賃貸営業課 宅地建物取引士")
s.text("090-1234-5678 yamada@example.jp")
s.text("03-1234-9999")
s.pb("naiken=edit")
s.pb("naiken=edit_guide")
s.text("鈴木")
s.pb("naiken=send")
print("\n=== 最近の物件から選ぶ ===")
s.pb("naiken=start")
s.pb("naiken=recent")
print("\n=== 自由入力 ===")
s.text("内見FAX グリーンヒル阿佐谷 301 10/29 14時 ひかり不動産管理 090-1111-2222")
s.text("03-0000-1111")
s.pb("naiken=send")
s.pb("cancel")
