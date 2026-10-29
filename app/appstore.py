"""App Store の課金（iPhoneアプリ版）。StoreKit 2 の署名付き取引（JWS）を確かめて、プランや追加FAXに反映する。

JWS は Apple の証明書チェーン（x5c）付きで届くので、ルート証明書が Apple Root CA - G3 であることと、
署名が正しいことをサーバーで確かめる（App Store Server API の鍵は不要）。
定期購入の更新・解約・プラン変更は App Store Server Notifications V2（/v1/billing/apple/notify）で受け取る。
"""
import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

import config
import store

log = logging.getLogger("appstore")
BUNDLE_ID = config.env("IOS_BUNDLE_ID", "com.otpromo.ippunshiryo")
SUBS = {"lite_monthly": "light", "unlimited_monthly": "unlimited",
        "lite_yearly": "light", "unlimited_yearly": "unlimited"}  # Play と同じ商品ID
FAX_PACK = "fax_10"
FAXRECV_PACK = "fax_receive_10"
# Apple Root CA - G3 の SHA-256 指紋
APPLE_ROOT_G3 = "63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179"
OID_LEAF = "1.2.840.113635.100.6.11.1"
OID_WWDR = "1.2.840.113635.100.6.2.1"


class Invalid(Exception):
    pass


def _b64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def decode(jws: str) -> dict:
    """署名付きデータ（JWS）を確かめて中身を返す。おかしければ Invalid。"""
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        h64, p64, s64 = jws.split(".")
        head = json.loads(_b64(h64))
        certs = [x509.load_der_x509_certificate(base64.b64decode(c)) for c in head["x5c"]]
    except Exception as e:
        raise Invalid(f"形式が違います: {e}")
    if head.get("alg") != "ES256" or len(certs) != 3:
        raise Invalid("署名の種類が違います")
    leaf, mid, root = certs
    if root.fingerprint(hashes.SHA256()).hex() != APPLE_ROOT_G3:
        raise Invalid("Appleのルート証明書ではありません")
    exts = lambda c: {e.oid.dotted_string for e in c.extensions}
    if OID_LEAF not in exts(leaf) or OID_WWDR not in exts(mid):
        raise Invalid("App Store の証明書ではありません")
    now = datetime.now(timezone.utc)
    try:
        for child, parent in ((leaf, mid), (mid, root)):
            if not (child.not_valid_before_utc <= now <= child.not_valid_after_utc):
                raise Invalid("証明書の期限が切れています")
            parent.public_key().verify(child.signature, child.tbs_certificate_bytes,
                                       ec.ECDSA(child.signature_hash_algorithm))
        sig = _b64(s64)
        r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
        leaf.public_key().verify(encode_dss_signature(r, s), f"{h64}.{p64}".encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise Invalid("署名が正しくありません")
    return json.loads(_b64(p64))


def _ms(v):
    return datetime.fromtimestamp(v / 1000, timezone.utc) if v else None


def verify(uid, product_id, jws):
    """アプリで購入した直後に呼ぶ。戻り値 {"ok": bool, "plan"?: ..., "error"?: ...}"""
    try:
        tx = decode(jws)
    except Invalid as e:
        return {"ok": False, "error": f"確認できませんでした（{e}）"}
    if tx.get("bundleId") != BUNDLE_ID or tx.get("productId") != product_id:
        return {"ok": False, "error": "購入の内容が違います"}
    if tx.get("revocationDate"):
        return {"ok": False, "error": "返金済みの購入です"}
    if product_id in (FAX_PACK, FAXRECV_PACK):
        key = hashlib.sha256(f"apple:{tx['transactionId']}".encode()).hexdigest()
        # 同じ取引で二重に足さない（Play と同じ記録の場所を使う）
        if not store.db().create("play_tokens", key, {"uid": uid, "product": product_id, "store": "app_store",
                                                     "at": store.now()}):
            return {"ok": True, "duplicate": True}
        if product_id == FAXRECV_PACK:
            import faxopt
            faxopt.add_pack(uid)
        else:
            import naiken
            naiken.add_pack(uid)
        return {"ok": True}
    if product_id in SUBS:
        return apply_subscription(uid, tx)
    return {"ok": False, "error": "不明な商品です"}


def apply_subscription(uid, tx, renewal=None):
    """定期購入の取引から、今のプランを利用者に書き込む。uid が None なら最初の取引IDで探す。"""
    otx = str(tx.get("originalTransactionId") or "")
    if uid is None:
        uid, _ = store.db().find("users", "apple_original_tx", otx)
    if not uid:
        return {"ok": False, "error": "利用者が見つかりません"}
    pid = tx.get("productId") or ""
    end = _ms(tx.get("expiresDate"))
    active = bool(end and end > datetime.now(timezone.utc) and not tx.get("revocationDate"))
    yearly = pid.endswith("_yearly")
    data = {"plan": SUBS.get(pid, "free") if active else "free", "plan_source": "app_store",
            "apple_original_tx": otx, "apple_env": tx.get("environment"), "period_end": end,
            "billing_interval": "year" if yearly else "month",
            "period_start": _ms(tx.get("purchaseDate")) or ((store._add_months(end, -12) if yearly else end - timedelta(days=30)) if end else None)}
    if renewal is not None:
        # 自動更新を止めた（解約）・次の更新から別のプランに変える（下げる・払い方の変更）
        data["cancel_at_period_end"] = renewal.get("autoRenewStatus") == 0
        data["apple_next_product"] = renewal.get("autoRenewProductId")
    before = store.get_user(uid) or {}
    if data["plan"] != "free":
        data["paid_before"] = True
    store.update_user(uid, data)
    # 紹介の特典は Play と同じ扱い（アプリ版は内見FAX+10回）
    if data["plan"] != "free" and (before.get("plan") or "free") == "free":
        import referral
        referral.release_app_pending(uid)
    if data["plan"] != "free" and not before.get("paid_before"):
        import bot
        import referral
        for nuid, mid, kw in referral.on_play_paid(uid):
            try:
                bot.notify(nuid, mid, kw)
            except Exception:
                log.exception("紹介のお知らせに失敗")
    return {"ok": True, "plan": data["plan"]}


def notify(body: dict):
    """App Store Server Notifications V2。{"signedPayload": JWS} が届く。"""
    try:
        n = decode(body.get("signedPayload") or "")
    except Invalid as e:
        log.warning("App Store 通知を捨てました: %s", e)
        return False
    d = n.get("data") or {}
    if d.get("bundleId") not in (None, BUNDLE_ID):
        return False
    try:
        tx = decode(d["signedTransactionInfo"]) if d.get("signedTransactionInfo") else None
        renewal = decode(d["signedRenewalInfo"]) if d.get("signedRenewalInfo") else None
    except Invalid as e:
        log.warning("App Store 通知の中身を確かめられません: %s", e)
        return False
    if tx and tx.get("productId") in SUBS:
        res = apply_subscription(None, tx, renewal)
        log.info("App Store 通知 %s/%s %s", n.get("notificationType"), n.get("subtype"), res)
        if (n.get("notificationType"), n.get("subtype")) == ("DID_CHANGE_RENEWAL_STATUS", "AUTO_RENEW_DISABLED"):
            # 自動更新をオフ＝解約の手続き。アプリで解約の理由を1回聞く（/v1/me の ask_cancel_reason）
            uid, _ = store.db().find("users", "apple_original_tx", str(tx.get("originalTransactionId") or ""))
            if uid:
                store.update_user(uid, {"ask_cancel_reason": True})
    elif tx and n.get("notificationType") == "REFUND":
        log.warning("消費型の返金 %s %s", tx.get("productId"), tx.get("transactionId"))
    return True
