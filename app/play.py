"""Google Play の課金（アプリ版）。購入トークンを Google Play Developer API で確かめて、プランや追加FAXに反映する。

Cloud Run のサービスアカウントを Play Console の「ユーザーと権限」に招待しておく必要がある（財務データの閲覧・注文の管理）。
"""
import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

import config
import store

log = logging.getLogger("play")
PACKAGE = config.env("ANDROID_PACKAGE", "com.otpromo.ippunshiryo")
API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
SUBS = {"lite_monthly": "light", "unlimited_monthly": "unlimited"}
FAX_PACK = "fax_10"


def _session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/androidpublisher"])
    return AuthorizedSession(creds)


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def verify(uid, product_id, token):
    """戻り値 {"ok": bool, "plan"?: ..., "error"?: ...}"""
    sess = _session()
    key = hashlib.sha256(token.encode()).hexdigest()
    if product_id == FAX_PACK:
        r = sess.get(f"{API}/{PACKAGE}/purchases/products/{FAX_PACK}/tokens/{token}")
        if r.status_code != 200:
            return {"ok": False, "error": f"確認できませんでした（{r.status_code}）"}
        p = r.json()
        if p.get("purchaseState") != 0:
            return {"ok": False, "error": "購入が完了していません"}
        # 同じトークンで二重に足さない
        if not store.db().create("play_tokens", key, {"uid": uid, "product": FAX_PACK, "at": store.now()}):
            return {"ok": True, "duplicate": True}
        import naiken
        naiken.add_pack(uid)
        if p.get("consumptionState") != 1:
            sess.post(f"{API}/{PACKAGE}/purchases/products/{FAX_PACK}/tokens/{token}:consume")
        return {"ok": True}
    if product_id in SUBS:
        return sync_subscription(uid, token, sess)
    return {"ok": False, "error": "不明な商品です"}


def sync_subscription(uid, token, sess=None):
    sess = sess or _session()
    r = sess.get(f"{API}/{PACKAGE}/purchases/subscriptionsv2/tokens/{token}")
    if r.status_code != 200:
        return {"ok": False, "error": f"確認できませんでした（{r.status_code}）"}
    s = r.json()
    item = (s.get("lineItems") or [{}])[0]
    pid = item.get("productId")
    state = s.get("subscriptionState", "")
    active = state in ("SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD")
    if uid is None:
        uid, _ = store.db().find("users", "play_token", token)
        if not uid:
            linked = (s.get("linkedPurchaseToken"))
            uid, _ = store.db().find("users", "play_token", linked) if linked else (None, None)
    if not uid:
        return {"ok": False, "error": "利用者が見つかりません"}
    end = _ts(item.get("expiryTime"))
    data = {"plan": SUBS.get(pid, "free") if active else "free", "plan_source": "google_play",
            "play_token": token, "play_state": state, "period_end": end,
            # 月額なので、期限の1か月前を今の期間の始まりとみなす（枚数の付与の区切りに使う）
            "period_start": (end - timedelta(days=30)) if end else None}
    store.update_user(uid, data)
    if s.get("acknowledgementState") == "ACKNOWLEDGEMENT_STATE_PENDING":
        sess.post(f"{API}/{PACKAGE}/purchases/subscriptions/{pid}/tokens/{token}:acknowledge", json={})
    return {"ok": True, "plan": data["plan"]}


def rtdn(body: dict):
    """Pub/Sub の push で届く「リアルタイム デベロッパー通知」。"""
    msg = (body.get("message") or {}).get("data")
    if not msg:
        return
    n = json.loads(base64.b64decode(msg))
    sn = n.get("subscriptionNotification")
    if sn and sn.get("purchaseToken"):
        res = sync_subscription(None, sn["purchaseToken"])
        log.info("RTDN %s %s", sn.get("notificationType"), res)
