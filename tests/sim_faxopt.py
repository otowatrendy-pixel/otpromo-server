"""受け取り用FAXオプションを手元でまねる（FAX送信はまね、超過の請求はログだけ）。"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-chintai.json"))
import simulate as s  # 登録とマイソク10枚まで流れる
import billing, delivery, faxopt, store
sent = []
delivery.fax_ready = lambda: True
delivery.send_fax = lambda num, pdf: sent.append((num, len(pdf), pdf[:4])) or True
billing.fax_overage = lambda uid, pages, yen: print(f"[請求] 超過{pages}ページ×{yen}円")
u = store.get_user("LUabc")
store.update_user("LUabc", {"fax_option": True, "free_limit": 100,
                            "delivery": {**u["delivery"], "method": "fax", "fax": "03-1234-9999"}})
k = store.period_key(store.get_user("LUabc"))
store.update_user("LUabc", {"faxopt_count": {k: 34}})
for i in range(4):
    print(f"\n=== {i + 1}件目（送る前 {store.get_user('LUabc')['faxopt_count'][k]}ページ） ===")
    s.image(s.img)
print("\n送ったFAX:", sent)
s.pb("faxopt=pause")
s.image(s.img)
print("一時停止後に送ったFAX:", len(sent))
s.pb("send=fax")
