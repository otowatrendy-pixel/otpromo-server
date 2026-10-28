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
SUBS = {"lite_monthly": "light", "unlimited_monthly": "unlimited",
        "lite_yearly": "light", "unlimited_yearly": "unlimited"}  # 年払いも件数・回数は毎月戻す
FAX_PACK = "fax_10"            # 内見FAXの追加 10回（消費型）
FAXRECV_PACK = "fax_receive_10"  # 受け取り用FAXの回数券 10回（消費型）


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
    if product_id in (FAX_PACK, FAXRECV_PACK):
        r = sess.get(f"{API}/{PACKAGE}/purchases/products/{product_id}/tokens/{token}")
        if r.status_code != 200:
            return {"ok": False, "error": f"確認できませんでした（{r.status_code}）"}
        p = r.json()
        if p.get("purchaseState") != 0:
            return {"ok": False, "error": "購入が完了していません"}
        # 同じトークンで二重に足さない
        if not store.db().create("play_tokens", key, {"uid": uid, "product": product_id, "at": store.now()}):
            return {"ok": True, "duplicate": True}
        if product_id == FAXRECV_PACK:
            import faxopt
            faxopt.add_pack(uid)
        else:
            import naiken
            naiken.add_pack(uid)
        if p.get("consumptionState") != 1:
            sess.post(f"{API}/{PACKAGE}/purchases/products/{product_id}/tokens/{token}:consume")
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
        linked = s.get("linkedPurchaseToken")
        if not uid and linked:
            uid, _ = store.db().find("users", "play_token", linked)
    if not uid:
        return {"ok": False, "error": "利用者が見つかりません"}
    end = _ts(item.get("expiryTime"))
    yearly = (pid or "").endswith("_yearly")
    data = {"plan": SUBS.get(pid, "free") if active else "free", "plan_source": "google_play",
            "play_token": token, "play_state": state, "period_end": end,
            "billing_interval": "year" if yearly else "month",
            # 期限の1か月前（年払いは1年前）を今の期間の始まりとみなす（枚数の付与の区切りに使う）
            "period_start": (store._add_months(end, -12) if yearly else end - timedelta(days=30)) if end else None}
    before = store.get_user(uid) or {}
    if data["plan"] != "free":
        data["paid_before"] = True
    store.update_user(uid, data)
    if data["plan"] != "free" and (before.get("plan") or "free") == "free":
        import referral
        referral.release_app_pending(uid)  # 無料体験中に紹介した分の特典を、ここで渡す
    if data["plan"] != "free" and not before.get("paid_before"):
        # 紹介された方の初めての有料プラン：双方に特典（アプリは内見FAX+10回）
        import bot
        import referral
        for nuid, mid, kw in referral.on_play_paid(uid):
            try:
                bot.notify(nuid, mid, kw)
            except Exception:
                log.exception("紹介のお知らせに失敗")
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
        if sn.get("notificationType") == 3:  # SUBSCRIPTION_CANCELED：アプリで解約の理由を1回聞く
            uid, _ = store.db().find("users", "play_token", sn["purchaseToken"])
            if uid:
                store.update_user(uid, {"ask_cancel_reason": True})
        log.info("RTDN %s %s", sn.get("notificationType"), res)
