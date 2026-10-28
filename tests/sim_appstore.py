"""App Store の課金（iPhone版）を手元でまねる。Apple の証明書チェーンの代わりに、手元で作った3段の証明書で署名する。"""
import base64, json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("STORE", "local")
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID
import types
sys.modules.setdefault("bot", types.SimpleNamespace(notify=lambda *a: None))  # 手元では LINE に送らない
import appstore, store

now = datetime.now(timezone.utc)


def cert(name, key, issuer=None, ikey=None, oid=None, ca=False):
    b = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
         .issuer_name(issuer or x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if oid:
        b = b.add_extension(x509.UnrecognizedExtension(x509.ObjectIdentifier(oid), b"\x05\x00"), critical=False)
    return b.sign(ikey or key, hashes.SHA256())


rk, mk, lk = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
root = cert("Fake Root", rk, ca=True)
mid = cert("Fake WWDR", mk, root.subject, rk, appstore.OID_WWDR, ca=True)
leaf = cert("Fake Leaf", lk, mid.subject, mk, appstore.OID_LEAF)
appstore.APPLE_ROOT_G3 = root.fingerprint(hashes.SHA256()).hex()  # 手元のルートを Apple の代わりにする
b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sign(payload, key=lk):
    head = {"alg": "ES256", "x5c": [base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode() for c in (leaf, mid, root)]}
    msg = f"{b64u(json.dumps(head).encode())}.{b64u(json.dumps(payload).encode())}"
    r, s = decode_dss_signature(key.sign(msg.encode(), ec.ECDSA(hashes.SHA256())))
    return f"{msg}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


ms = lambda d: int(d.timestamp() * 1000)
uid = "FBios1"
store.db().set("users", uid, {"firebase_uid": uid, "plan": "free"})
tx = {"bundleId": "com.otpromo.ippunshiryo", "productId": "lite_monthly", "transactionId": "2001",
      "originalTransactionId": "1000", "purchaseDate": ms(now), "expiresDate": ms(now + timedelta(days=30)),
      "environment": "Sandbox"}

print("=== ライトを購入 ===")
print(appstore.verify(uid, "lite_monthly", sign(tx)))
assert store.get_user(uid)["plan"] == "light" and store.get_user(uid)["plan_source"] == "app_store"

print("=== 別の鍵で署名したもの・商品IDが違うものは受け付けない ===")
print(appstore.verify(uid, "lite_monthly", sign(tx, ec.generate_private_key(ec.SECP256R1()))))
print(appstore.verify(uid, "unlimited_monthly", sign(tx)))
assert not appstore.verify(uid, "unlimited_monthly", sign(tx))["ok"]

print("=== 使い放題に上げた（通知で届く） ===")
up = {**tx, "productId": "unlimited_monthly", "transactionId": "2002"}
appstore.notify({"signedPayload": sign({"notificationType": "DID_CHANGE_RENEWAL_PREF", "subtype": "UPGRADE",
                                        "data": {"bundleId": tx["bundleId"], "signedTransactionInfo": sign(up),
                                                 "signedRenewalInfo": sign({"autoRenewStatus": 1, "autoRenewProductId": "unlimited_monthly"})}})})
assert store.get_user(uid)["plan"] == "unlimited"

print("=== 解約して期限が過ぎた ===")
gone = {**up, "expiresDate": ms(now - timedelta(minutes=1))}
appstore.notify({"signedPayload": sign({"notificationType": "EXPIRED", "data": {"signedTransactionInfo": sign(gone),
                                        "signedRenewalInfo": sign({"autoRenewStatus": 0})}})})
assert store.get_user(uid)["plan"] == "free"

print("=== 追加FAXは同じ取引で二重に足さない ===")
fx = {"bundleId": tx["bundleId"], "productId": "fax_10", "transactionId": "3001", "originalTransactionId": "3001"}
print(appstore.verify(uid, "fax_10", sign(fx)), appstore.verify(uid, "fax_10", sign(fx)))
assert appstore.verify(uid, "fax_10", sign(fx)).get("duplicate")
print("OK")
