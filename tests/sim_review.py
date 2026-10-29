"""ストア審査用のアカウント：最初のログインから使い放題、FAXは送ったことにする、名前の判定・紹介・日報から外す。"""
import os, sys
from datetime import timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("INVOICE_TRN", "")
from fastapi.testclient import TestClient
import main, store, delivery, naiken, faxopt, usage, growth, referral
c = TestClient(main.app)
H = {"Authorization": "Bearer dev:review:abc"}
me = c.get("/v1/me", headers=H).json()
print(me["plan"], me.get("naiken_pack_left"), me.get("fax_receive"))
assert me["plan"] == "unlimited"
uid, u = store.user_by_firebase("review:abc")
assert u["review"] and faxopt.left(u) == 100 and naiken.pack_left(u) == 100
# ふつうの方は対象外
assert c.get("/v1/me", headers={"Authorization": "Bearer dev:someone"}).json()["plan"] == "free"
# 名前の判定をしない（それっぽくない名前でも通る）
assert u["registered"] and u["company"]["担当者名"] == "山田 太郎"
import review
r = c.put("/v1/me/company", headers=H, json={**{k: v for k, v in review.COMPANY.items() if k not in ("メール", "帯画像", "名刺済み")}, "担当者名": "てすと"})
assert r.status_code == 200, r.text
print("会社情報:", r.status_code, str(r.json())[:120])
# FAXは送ったことにする
d = {"to": {"company": "テスト管理", "fax": "03-1234-5678"}, "prop": {"name": "テスト", "room": "1"}, "wishes": [{"date": "11/12", "time": "14:00"}]}
fid, _ = naiken.save_preview(uid, d, store.get_user(uid)["company"])
print("内見FAX:", naiken.send(uid, fid))
for pid, _ in store.db().all("fax_sends"):
    store.db().set("fax_sends", pid, {"next_check": store.now() - timedelta(seconds=1)}, merge=True)
delivery.poll_faxes()
assert store.db().get("faxes", fid)["status"] == "sent"
# 日報・週報・紹介から外す
assert usage.user_stats()["paid_total"] == 0
assert growth.funnel()["paid"] == 0
# 受け取り用FAXも送ったことにする
u = store.get_user(uid)
print("受取りFAX:", faxopt.left(u))
print("OK")
