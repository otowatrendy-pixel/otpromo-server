"""内見FAXの会話を手元でまねる（日時の読み取りは本物のAIを使う）。"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-chintai.json"))
import simulate as s  # 登録とマイソク10枚まで流れる
import store
store.update_user("LUabc", {"free_limit": 100})
s.image(s.img)
s.pb("naiken=from_last")
s.text("明日の14時と、土曜の午前 管理はひかり不動産管理 03-0000-1111")
s.text("山田 花子")
s.pb("card_roman=none")
s.text("賃貸営業課 宅地建物取引士")
s.text("090-1234-5678 yamada@example.jp")
s.text("03-1234-9999")
s.pb("naiken=edit")
s.pb("naiken=edit_guests")
s.text("2名")
s.pb("naiken=send")
print("\n=== 自由入力 ===")
s.text("内見FAX グリーンヒル阿佐谷 301 10/29 14時 ひかり不動産管理 090-1111-2222")
s.text("03-0000-1111")
s.pb("naiken=send")
s.pb("cancel")
