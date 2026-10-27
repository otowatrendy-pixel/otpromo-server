"""Stripe の支払い（Checkout）・お客様ページ・Webhook。

ライト（1,480円）・使い放題（2,980円）は税込、FAXオプションは税別。消費税10%の税率（txr_）を項目ごとに付ける。
"""
import base64
import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone

import config
import store

log = logging.getLogger("billing")
PLAN_NAMES = {"free": "無料お試し", "light": "ライトプラン", "unlimited": "使い放題プラン"}


def _stripe():
    import stripe
    stripe.api_key = config.STRIPE_SECRET_KEY
    return stripe


def plain(o):
    """Stripeの戻り値を普通の dict にする（新しいSDKは dict ではないため）。"""
    return o.to_dict() if hasattr(o, "to_dict") else o


# ---------- リンクの署名（ログインなしでフォームやお支払いを開くため） ----------

def sign(uid, days=7):
    exp = int(time.time()) + days * 86400
    body = f"{uid}|{exp}"
    mac = hmac.new(config.APP_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{body}|{mac}".encode()).decode().rstrip("=")


def unsign(t):
    try:
        uid, exp, mac = base64.urlsafe_b64decode(t + "=" * (-len(t) % 4)).decode().rsplit("|", 2)
    except Exception:
        return None
    good = hmac.new(config.APP_SECRET.encode(), f"{uid}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(good, mac) or int(exp) < time.time():
        return None
    return uid


def fax_pack_price():
    """追加FAX（10回500円・税込）の価格。PRICE_FAX_PACK がなければ lookup_key で探し、なければ作る。"""
    pid = config.env("PRICE_FAX_PACK")
    if pid:
        return pid
    s = _stripe()
    found = plain(s.Price.list(lookup_keys=["naiken_fax_10"], limit=1))["data"]
    if found:
        return found[0]["id"]
    prod = plain(s.Product.create(name="内見申込FAX 追加10回", description="購入から3か月有効（税込）",
                                  metadata={"service": "maisoku", "kind": "fax_pack"}))
    return plain(s.Price.create(product=prod["id"], currency="jpy", unit_amount=500, tax_behavior="inclusive",
                                lookup_key="naiken_fax_10", nickname="追加FAX 10回 500円（税込）"))["id"]


def fax_pack_checkout(uid):
    s = _stripe()
    u = store.get_user(uid)
    params = dict(mode="payment", line_items=[{"price": fax_pack_price(), "quantity": 1,
                                               "tax_rates": [tax_rate(inclusive=True)]}],
                  client_reference_id=uid, metadata={"uid": uid, "purpose": "fax_pack"}, locale="ja",
                  success_url=f"{config.BASE_URL}/paid?t={sign(uid)}", cancel_url=f"{config.BASE_URL}/canceled")
    if u.get("stripe_customer"):
        params["customer"] = u["stripe_customer"]
    return plain(s.checkout.Session.create(**params))["url"]


def pay_url(uid, plan):
    return f"{config.BASE_URL}/pay/{plan}?t={sign(uid)}"


def portal_url(uid):
    return f"{config.BASE_URL}/portal?t={sign(uid)}"


# ---------- 税率 ----------
# ライト・使い放題は税込価格、FAXオプションは税別価格。項目ごとに税込・税別の消費税10%を付ける

_tax = {}


def tax_rate(inclusive=False):
    """消費税10%の税率（txr_）。Stripeから探し、なければ作る。"""
    if not inclusive and config.TAX_RATE:
        return config.TAX_RATE
    if inclusive in _tax:
        return _tax[inclusive]
    s = _stripe()
    tag = "jct10-in" if inclusive else "jct10"
    for r in s.TaxRate.list(active=True, limit=100).auto_paging_iter():
        r = plain(r)
        if r.get("metadata", {}).get("otpromo") == tag:
            _tax[inclusive] = r["id"]
            return r["id"]
    r = s.TaxRate.create(display_name="消費税", percentage=10, inclusive=inclusive, country="JP",
                         jurisdiction="JP", description="消費税10%" + ("（内税）" if inclusive else ""),
                         metadata={"otpromo": tag})
    _tax[inclusive] = r["id"]
    return r["id"]


def item_tax(price):
    """その価格に付ける税率。ライトと使い放題は税込。"""
    return [tax_rate(inclusive=(price in (config.PRICE_LIGHT, config.PRICE_UNLIMITED)))]


# ---------- 申し込み ----------

def checkout(uid, plan, fax=False):
    """プランの支払いページ（Checkout）のURLを作る。すでに契約中なら None。"""
    s = _stripe()
    u = store.get_user(uid)
    price = {"light": config.PRICE_LIGHT, "unlimited": config.PRICE_UNLIMITED}[plan]
    items = [{"price": price, "quantity": 1, "tax_rates": item_tax(price)}]
    if fax:
        items.append({"price": config.PRICE_FAX, "quantity": 1, "tax_rates": item_tax(config.PRICE_FAX)})
    company = (u.get("company") or {}).get("会社名") or ""
    params = dict(
        mode="subscription", line_items=items, client_reference_id=uid,
        subscription_data={"metadata": {"uid": uid}},
        metadata={"uid": uid, "plan": plan},
        success_url=f"{config.BASE_URL}/paid?t={sign(uid)}",
        cancel_url=f"{config.BASE_URL}/canceled",
        locale="ja",
    )
    if u.get("stripe_customer"):
        params["customer"] = u["stripe_customer"]
    elif u.get("delivery", {}).get("email"):
        params["customer_email"] = u["delivery"]["email"]
    sess = s.checkout.Session.create(**params)
    log.info("checkout %s %s %s", uid, plan, company)
    return sess.url


def change_plan(uid, plan=None, fax=None):
    """契約中の内容を変える（ライト⇔使い放題、FAXオプションの追加）。日割りで精算。"""
    s = _stripe()
    u = store.get_user(uid)
    sub = plain(s.Subscription.retrieve(u["stripe_subscription"]))
    items = []
    for it in sub["items"]["data"]:
        pid = it["price"]["id"]
        if plan and pid in (config.PRICE_LIGHT, config.PRICE_UNLIMITED):
            new = config.PRICE_UNLIMITED if plan == "unlimited" else config.PRICE_LIGHT
            items.append({"id": it["id"], "price": new, "tax_rates": item_tax(new)})
    if fax and not any(it["price"]["id"] == config.PRICE_FAX for it in sub["items"]["data"]):
        items.append({"price": config.PRICE_FAX, "quantity": 1, "tax_rates": item_tax(config.PRICE_FAX)})
    if items:
        s.Subscription.modify(sub["id"], items=items, proration_behavior="create_prorations")
    sync_subscription(s.Subscription.retrieve(sub["id"]))


def portal(uid):
    s = _stripe()
    u = store.get_user(uid)
    if not u.get("stripe_customer"):
        return None
    sess = s.billing_portal.Session.create(customer=u["stripe_customer"], locale="ja",
                                           return_url=f"{config.BASE_URL}/paid?t={sign(uid)}")
    return sess.url


# ---------- 契約の状態を利用者に写す ----------

def _period(sub):
    """今の請求期間（新しいAPIでは項目ごとに持つ）。"""
    it = sub["items"]["data"][0] if sub["items"]["data"] else {}
    start = sub.get("current_period_start") or it.get("current_period_start")
    end = sub.get("current_period_end") or it.get("current_period_end")
    return start, end


def sync_subscription(sub):
    """Stripeの契約内容から、利用者のプラン・期間・FAXオプションを決める。戻り値は (uid, 変更前のプラン, 変更後)。"""
    sub = plain(sub)
    uid = (sub.get("metadata") or {}).get("uid")
    if not uid:
        uid, _ = store.db().find("users", "stripe_customer", sub["customer"])
    if not uid:
        log.warning("利用者が見つからない契約 %s", sub["id"])
        return None, None, None
    u = store.get_user(uid)
    prices = [it["price"]["id"] for it in sub["items"]["data"]]
    active = sub["status"] in ("active", "trialing", "past_due")
    plan = "free"
    if active:
        plan = "unlimited" if config.PRICE_UNLIMITED in prices else "light" if config.PRICE_LIGHT in prices else "free"
    start, end = _period(sub)
    data = {"plan": plan, "plan_source": "stripe" if plan != "free" else u.get("plan_source"),
            "fax_option": active and config.PRICE_FAX in prices,
            "stripe_customer": sub["customer"], "stripe_subscription": sub["id"] if active else None,
            "sub_status": sub["status"], "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
            "period_start": datetime.fromtimestamp(start, timezone.utc) if start else None,
            "period_end": datetime.fromtimestamp(end, timezone.utc) if end else None}
    store.update_user(uid, data)
    return uid, u.get("plan"), plan


def handle_webhook(payload: bytes, sig: str):
    """Stripeからの通知。戻り値は LINE で知らせる内容 [(uid, 文面ID, 差し込み)]。"""
    s = _stripe()
    ev = plain(s.Webhook.construct_event(payload, sig, config.STRIPE_WEBHOOK_SECRET))
    if not store.first_event("stripe-" + ev["id"]):
        return []
    t, obj = ev["type"], ev["data"]["object"]
    notices = []
    if t == "checkout.session.completed" and obj.get("mode") == "subscription":
        uid = obj.get("client_reference_id")
        store.update_user(uid, {"stripe_customer": obj["customer"]})
        sub = s.Subscription.retrieve(obj["subscription"])
        uid, before, after = sync_subscription(sub)
        if uid:
            notices.append((uid, "plan_started", {}))
    elif t == "checkout.session.completed" and (obj.get("metadata") or {}).get("purpose") == "fax_pack":
        import naiken
        uid = obj.get("client_reference_id")
        if uid and obj.get("payment_status") == "paid":
            naiken.add_pack(uid)
            exp = store.now() + timedelta(days=naiken.PACK_DAYS)
            d = exp.astimezone(store.JST)
            notices.append((uid, "addfax_done", {"有効期限": f"{d.month}月{d.day}日（{'月火水木金土日'[d.weekday()]}）"}))
    elif t == "checkout.session.completed" and obj.get("mode") == "setup":
        import monitor
        n = monitor.on_setup_completed(obj)
        if n:
            notices.append(n)
    elif t in ("customer.subscription.updated", "customer.subscription.created"):
        uid, before, after = sync_subscription(obj)
        if uid and before not in (None, after) and after != "free" and t == "customer.subscription.updated":
            notices.append((uid, "plan_started", {}))
        prev = (ev["data"].get("previous_attributes") or {})
        if uid and obj.get("cancel_at_period_end") and prev.get("cancel_at_period_end") is False:
            notices.append((uid, "plan_canceled", {}))
    elif t == "customer.subscription.deleted":
        uid, before, after = sync_subscription(obj)
    elif t == "invoice.payment_failed":
        uid, _ = store.db().find("users", "stripe_customer", obj["customer"])
        if uid:
            notices.append((uid, "plan_payment_failed", {}))
    elif t == "invoice.paid":
        sid = obj.get("subscription") or ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
        if sid:
            sync_subscription(s.Subscription.retrieve(sid))
    return notices
