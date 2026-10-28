"""Stripe の支払い（Checkout）・お客様ページ・Webhook。

ライト（1,480円）・使い放題（2,980円）・回数券（内見FAX追加・受け取り用FAX）は、どれも税込。消費税10%の税率（txr_）を項目ごとに付ける。
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


def plan_name(u):
    """表示用のプラン名。年払いは「ライトプラン（年払い）」。"""
    n = PLAN_NAMES[u.get("plan") or "free"]
    if (u.get("plan") or "free") != "free" and u.get("plan_source") == "group" and u.get("shop_name"):
        return f"{n}（{u['shop_name']}・店舗まとめ割）"
    return n + "（年払い）" if (u.get("plan") or "free") != "free" and u.get("billing_interval") == "year" else n


# プランの価格。本番のキーに替えたときも、起動時に lookup_key で探して、なければ作る（ensure_prices）
PLAN_PRICES = [  # (config の名前, lookup_key, 商品, 税込の金額, 間隔, 呼び名)
    ("PRICE_LIGHT", "light_monthly", "light", 1480, "month", "ライト 月額1,480円（税込）"),
    ("PRICE_UNLIMITED", "unlimited_monthly", "unlimited", 2980, "month", "使い放題 月額2,980円（税込）"),
    ("PRICE_LIGHT_YEAR", "light_yearly", "light", 14800, "year", "ライト 年額14,800円（税込）"),
    ("PRICE_UNLIMITED_YEAR", "unlimited_yearly", "unlimited", 29800, "year", "使い放題 年額29,800円（税込）"),
    ("PRICE_GROUP_LIGHT", "group_light_monthly", "group", 1180, "month", "店舗まとめ割 ライト 1人1,180円（税込）"),
    ("PRICE_GROUP_UNLIMITED", "group_unlimited_monthly", "group", 2480, "month", "店舗まとめ割 使い放題 1人2,480円（税込）"),
]
PRODUCTS = {
    "light": {"name": "1分資料 ライトプラン",
              "description": "資料づくりは募集図面 月50件分、内見申込FAXは月20回までご利用いただけます。年払いでも件数・回数は毎月同じです。",
              "metadata": {"service": "maisoku", "plan": "light", "monthly_limit": "50", "naiken_fax_limit": "20"}},
    "unlimited": {"name": "1分資料 使い放題プラン",
                  "description": "資料づくりは件数を気にせず、内見申込FAXは月50回までご利用いただけます。年払いでも回数は毎月同じです。",
                  "metadata": {"service": "maisoku", "plan": "unlimited", "naiken_fax_limit": "50"}},
    "group": {"name": "1分資料 店舗まとめ割",
              "description": "同じ店舗で3人以上のお申し込み。1人あたりの月額（税込）、お申し込みから6か月間",
              "metadata": {"service": "maisoku", "kind": "group"}},
}


def ensure_prices():
    """プランの価格IDを、いまのキー（サンドボックス／本番）のものにそろえる。環境変数で指定したものはそのまま。"""
    import os
    if not config.STRIPE_SECRET_KEY:
        return
    s = _stripe()
    keys = [lk for _, lk, *_ in PLAN_PRICES]
    found = {p["lookup_key"]: p for p in plain(s.Price.list(lookup_keys=keys, active=True, limit=20))["data"]}
    products = {}
    for attr, lk, kind, amount, interval, nick in PLAN_PRICES:
        if os.environ.get(attr):
            continue
        p = found.get(lk)
        if not p:
            # これまでの価格（サンドボックス）が使えるなら、それに lookup_key を付ける
            try:
                cur = plain(s.Price.retrieve(getattr(config, attr)))
                if cur.get("active") and cur.get("unit_amount") == amount:
                    p = plain(s.Price.modify(cur["id"], lookup_key=lk))
            except s.InvalidRequestError:
                pass
        if not p:
            if not products:
                for pr in s.Product.list(active=True, limit=100).auto_paging_iter():
                    md = plain(pr).get("metadata") or {}
                    if md.get("service") == "maisoku":
                        products.setdefault(md.get("plan") or md.get("kind"), plain(pr)["id"])
            if kind not in products:
                info = PRODUCTS[kind]
                products[kind] = plain(s.Product.create(name=info["name"], description=info["description"],
                                                        metadata=info["metadata"],
                                                        statement_descriptor=config.DESCRIPTOR_SUBSCRIPTION))["id"]
            p = plain(s.Price.create(product=products[kind], currency="jpy", unit_amount=amount,
                                     recurring={"interval": interval}, tax_behavior="inclusive",
                                     lookup_key=lk, nickname=nick))
            log.info("価格を作りました %s %s", lk, p["id"])
        setattr(config, attr, p["id"])


def price_of(plan, yearly=False):
    return {("light", False): config.PRICE_LIGHT, ("unlimited", False): config.PRICE_UNLIMITED,
            ("light", True): config.PRICE_LIGHT_YEAR, ("unlimited", True): config.PRICE_UNLIMITED_YEAR}[(plan, yearly)]


def plan_of_price(pid):
    """価格IDから (プラン, 年払いか)。プランの価格でなければ (None, False)。"""
    for (plan, yearly) in (("light", False), ("unlimited", False), ("light", True), ("unlimited", True)):
        if pid == price_of(plan, yearly):
            return plan, yearly
    # 店舗まとめ割（1人あたりの月額）
    if pid == config.PRICE_GROUP_LIGHT:
        return "light", False
    if pid == config.PRICE_GROUP_UNLIMITED:
        return "unlimited", False
    return None, False


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
                  custom_text={"submit": {"message": (
                      "購入から3か月お使いいただけます。解約されたときは、お支払い済みの期間の終わりで使えなくなります"
                      f"（返金はありません）。お申し込みにより、利用規約（{config.TERMS_URL}）に同意したものとします。")}},
                  success_url=f"{config.BASE_URL}/paid?t={sign(uid)}", cancel_url=f"{config.BASE_URL}/canceled")
    if u.get("stripe_customer"):
        params["customer"] = u["stripe_customer"]
    return plain(s.checkout.Session.create(**_invoice_for_payment(params)))["url"]


def faxrecv_pack_price():
    """受け取り用FAXの回数券（10回1,000円・税込）の価格。PRICE_FAXRECV_PACK がなければ lookup_key で探し、なければ作る。"""
    import faxopt
    pid = config.env("PRICE_FAXRECV_PACK")
    if pid:
        return pid
    s = _stripe()
    found = plain(s.Price.list(lookup_keys=["fax_receive_10"], limit=1))["data"]
    if found:
        return found[0]["id"]
    prod = plain(s.Product.create(name="1分資料 受け取り用FAX 10回",
                                  description="帯替え図面と初期費用概算書の2枚を御社のFAX機へ。1回100円（税込）、購入から3か月有効",
                                  metadata={"service": "maisoku", "kind": "faxrecv_pack"}))
    return plain(s.Price.create(product=prod["id"], currency="jpy", unit_amount=faxopt.PACK_YEN, tax_behavior="inclusive",
                                lookup_key="fax_receive_10", nickname="受け取り用FAX 10回 1,000円（税込）"))["id"]


def faxrecv_pack_checkout(uid):
    s = _stripe()
    u = store.get_user(uid)
    params = dict(mode="payment", line_items=[{"price": faxrecv_pack_price(), "quantity": 1,
                                               "tax_rates": [tax_rate(inclusive=True)]}],
                  client_reference_id=uid, metadata={"uid": uid, "purpose": "faxrecv_pack"}, locale="ja",
                  success_url=f"{config.BASE_URL}/paid?t={sign(uid)}", cancel_url=f"{config.BASE_URL}/canceled")
    if u.get("stripe_customer"):
        params["customer"] = u["stripe_customer"]
    return plain(s.checkout.Session.create(**_invoice_for_payment(params)))["url"]


def pay_url(uid, plan):
    return f"{config.BASE_URL}/pay/{plan}?t={sign(uid)}"


def portal_url(uid):
    return f"{config.BASE_URL}/portal?t={sign(uid)}"


# ---------- 税率 ----------
# 料金はどれも税込価格。項目ごとに内税の消費税10%を付ける（税別の税率は TAX_RATE 用に残す）

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
    """その価格に付ける税率。ライト・使い放題は税込。"""
    return [tax_rate(inclusive=plan_of_price(price)[0] is not None)]


# ---------- インボイスの登録番号 ----------

_trn = {}


def account_tax_ids():
    """請求書・領収書に載せる当社の登録番号（Stripeの tax_id）。設定がなければ空。"""
    if not config.INVOICE_TRN:
        return []
    if "id" not in _trn:
        try:
            _trn["id"] = _find_or_create_trn()
        except Exception:
            log.exception("登録番号を Stripe に用意できなかった（請求書には載らない）")
            return []
    return [_trn["id"]]


def _find_or_create_trn():
    s = _stripe()
    found = [t for t in (plain(x) for x in s.TaxId.list(owner={"type": "self"}, limit=20).auto_paging_iter())
             if t["type"] == "jp_trn" and t["value"] == config.INVOICE_TRN]
    return found[0]["id"] if found else plain(s.TaxId.create(type="jp_trn", value=config.INVOICE_TRN,
                                                               owner={"type": "self"}))["id"]


def _invoice_for_payment(params):
    """都度払い（追加FAXなど）にも、登録番号入りの請求書（領収書）を付ける。"""
    # カード明細に「OTOWA-T* OTPROMO」のようにサービス名を付ける（日本のカードはカナ・漢字の表記も）
    params.setdefault("payment_intent_data", {})["statement_descriptor_suffix"] = config.DESCRIPTOR_SUFFIX
    params.setdefault("payment_method_options", {})["card"] = {
        "statement_descriptor_suffix_kana": config.DESCRIPTOR_SUFFIX_KANA,
        "statement_descriptor_suffix_kanji": config.DESCRIPTOR_SUFFIX_KANJI}
    if account_tax_ids():
        params["invoice_creation"] = {"enabled": True, "invoice_data": {"account_tax_ids": account_tax_ids()}}
    return params


def ensure_descriptors():
    """契約（サブスク）の商品に、カード明細の表記を入れる。サブスクの支払いには都度払いのような
    明細の付け足しができないため、商品の statement_descriptor（ローマ字のみ）で表す。起動時に1回。"""
    s = _stripe()
    want = config.DESCRIPTOR_SUBSCRIPTION
    done = set()
    for price in (config.PRICE_LIGHT, config.PRICE_UNLIMITED, config.PRICE_LIGHT_YEAR, config.PRICE_UNLIMITED_YEAR,
                  config.PRICE_GROUP_LIGHT, config.PRICE_GROUP_UNLIMITED):
        if not price:
            continue
        prod = plain(s.Price.retrieve(price))["product"]
        if prod in done:
            continue
        done.add(prod)
        if plain(s.Product.retrieve(prod)).get("statement_descriptor") != want:
            s.Product.modify(prod, statement_descriptor=want)
    return sorted(done)


# ---------- 申し込み ----------

def checkout(uid, plan, yearly=False):
    """プランの支払いページ（Checkout）のURLを作る。yearly=True は年払い。"""
    s = _stripe()
    u = store.get_user(uid)
    price = price_of(plan, yearly)
    items = [{"price": price, "quantity": 1, "tax_rates": item_tax(price)}]
    company = (u.get("company") or {}).get("会社名") or ""
    params = dict(
        mode="subscription", line_items=items, client_reference_id=uid,
        subscription_data={"metadata": {"uid": uid}},
        metadata={"uid": uid, "plan": plan, "interval": "year" if yearly else "month"},
        success_url=f"{config.BASE_URL}/paid?t={sign(uid)}",
        cancel_url=f"{config.BASE_URL}/canceled",
        locale="ja",
        custom_text={"submit": {"message": f"お申し込みにより、利用規約（{config.TERMS_URL}）に同意したものとします。"}},
    )
    if u.get("stripe_customer"):
        params["customer"] = u["stripe_customer"]
    elif u.get("delivery", {}).get("email"):
        params["customer_email"] = u["delivery"]["email"]
    import campaign
    import referral
    newbie = not yearly and campaign.newbie_eligible(u)
    ref = referral.checkout_discount(u, plan, yearly, newbie)
    if ref:
        # 紹介割引（新人さん応援と重なったら大きいほうだけ）
        params["discounts"] = [{"coupon": ref}]
        params["metadata"]["referral"] = "1"
    elif newbie:
        # 新人さん応援：最初の月だけ半額（1回だけ50%引き）
        params["discounts"] = [{"coupon": newbie_coupon()}]
        params["metadata"]["newbie"] = "1"
    sess = s.checkout.Session.create(**params)
    log.info("checkout %s %s %s %s", uid, plan, "year" if yearly else "month", company)
    return sess.url


def newbie_coupon():
    """新人さん応援のクーポン（1回だけ50%引き）。なければ作る。"""
    s = _stripe()
    try:
        return plain(s.Coupon.retrieve(config.NEWBIE_COUPON))["id"]
    except s.InvalidRequestError:
        return plain(s.Coupon.create(id=config.NEWBIE_COUPON, name="新人さん応援 最初の月半額", percent_off=50,
                                     duration="once", metadata={"service": "maisoku", "kind": "newbie"}))["id"]


YEN = {("light", False): "月額1,480円（税込）", ("unlimited", False): "月額2,980円（税込）",
       ("light", True): "年額14,800円（税込）", ("unlimited", True): "年額29,800円（税込）"}


def change_kind(u, plan, yearly):
    """契約中の変更の種類（10/28本人決定）。
    up＝ライト→使い放題（払い方は同じ）：すぐ切り替えて差額を日割りで請求。
    later＝下げる・月払い⇔年払いの切り替え：次の更新日から。None＝変わらない。"""
    cur, cur_year = u.get("plan"), u.get("billing_interval") == "year"
    if (plan, yearly) == (cur, cur_year):
        return None
    if cur == "light" and plan == "unlimited" and yearly == cur_year:
        return "up"
    return "later"


def can_change(u, plan, yearly):
    return change_kind(u, plan, yearly) is not None


def _items_of(phase_or_sub_items):
    out = []
    for it in phase_or_sub_items:
        price = it["price"] if isinstance(it["price"], str) else it["price"]["id"]
        tr = [t if isinstance(t, str) else t["id"] for t in (it.get("tax_rates") or [])]
        out.append({"price": price, "quantity": it.get("quantity") or 1, **({"tax_rates": tr} if tr else {})})
    return out


def _release_schedule(s, sub):
    sch = sub.get("schedule")
    if sch:
        s.SubscriptionSchedule.release(sch if isinstance(sch, str) else sch["id"])


def change_plan(uid, plan, yearly=False):
    """契約中のプランを変える。戻り値は ("up", 差額) か ("later", 切り替わる日時)。"""
    s = _stripe()
    u = store.get_user(uid)
    sub = plain(s.Subscription.retrieve(u["stripe_subscription"]))
    new = price_of(plan, yearly)
    kind = change_kind(u, plan, yearly)
    _release_schedule(s, sub)  # 前に予約した変更は取り消して、今回の内容にする
    store.update_user(uid, {"pending_change": None})
    if kind == "up":
        items = [{"id": it["id"], "price": new, "tax_rates": item_tax(new)}
                 for it in sub["items"]["data"] if plan_of_price(it["price"]["id"])[0]]
        s.Subscription.modify(sub["id"], items=items, proration_behavior="always_invoice")
        sub2 = plain(s.Subscription.retrieve(sub["id"], expand=["latest_invoice"]))
        inv = sub2.get("latest_invoice") or {}
        sync_subscription(sub2)
        return "up", int(inv.get("amount_due") or 0)
    # 下げる・払い方を変える：次の更新日から（日割りの返金なし）
    sch = plain(s.SubscriptionSchedule.create(from_subscription=sub["id"]))
    cur = sch["phases"][0]
    now_items = _items_of(cur["items"])
    next_items = [({**it, "price": new, "tax_rates": item_tax(new)} if plan_of_price(it["price"])[0] else it)
                  for it in now_items]
    s.SubscriptionSchedule.modify(sch["id"], end_behavior="release", proration_behavior="none", phases=[
        {"items": now_items, "start_date": cur["start_date"], "end_date": cur["end_date"],
         **({"trial_end": cur["trial_end"]} if cur.get("trial_end") else {})},
        {"items": next_items, "proration_behavior": "none", "metadata": {"uid": uid}},
    ], metadata={"uid": uid, "purpose": "plan_change"})
    at = datetime.fromtimestamp(cur["end_date"], timezone.utc)
    store.update_user(uid, {"pending_change": {"plan": plan, "interval": "year" if yearly else "month", "at": at}})
    return "later", at


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
    if (sub.get("metadata") or {}).get("purpose") == "group":
        import campaign
        uid = campaign.sync_group(sub)
        _ensure_sub_tax_ids(sub, sub["status"] in ("active", "trialing", "past_due"))
        return uid, None, None
    uid = (sub.get("metadata") or {}).get("uid")
    if not uid:
        uid, _ = store.db().find("users", "stripe_customer", sub["customer"])
    if not uid:
        log.warning("利用者が見つからない契約 %s", sub["id"])
        return None, None, None
    u = store.get_user(uid)
    prices = [it["price"]["id"] for it in sub["items"]["data"]]
    active = sub["status"] in ("active", "trialing", "past_due")
    plan, yearly = "free", False
    if active:
        found = [plan_of_price(p) for p in prices if plan_of_price(p)[0]]
        if found:
            plan, yearly = found[0]
    start, end = _period(sub)
    start_dt = datetime.fromtimestamp(start, timezone.utc) if start else None
    data = {"plan": plan, "plan_source": "stripe" if plan != "free" else u.get("plan_source"),
            "billing_interval": "year" if yearly else "month",
            "stripe_customer": sub["customer"], "stripe_subscription": sub["id"] if active else None,
            "sub_status": sub["status"], "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
            "period_start": start_dt,
            "period_end": datetime.fromtimestamp(end, timezone.utc) if end else None}
    if yearly and start_dt and u.get("period_start") != start_dt:
        data["year_base_docs"] = u.get("total_docs", 0)  # 年払いの更新案内で「この1年の件数」を出すため
    pend = u.get("pending_change") or {}
    if pend and (not active or (pend.get("plan"), pend.get("interval")) == (plan, "year" if yearly else "month")):
        data["pending_change"] = None
    if plan != "free":
        data["ended_at"] = None
        data["paid_before"] = True  # 新人さん応援は「初めての方」だけ
    if plan == "free" and u.get("plan_source") in ("group", "group_leaving"):
        # 店舗まとめ割で使っている方は、個人の契約の状態でプランを上書きしない
        data = {"stripe_customer": sub["customer"], "stripe_subscription": None, "sub_status": sub["status"]}
    store.update_user(uid, data)
    _ensure_sub_tax_ids(sub, active)
    _track_yearly(uid, yearly and plan != "free")
    if plan == "free" and u.get("plan_source") == "stripe" and (u.get("plan") or "free") != "free":
        import referral
        referral.on_canceled(uid)  # 解約したら、まだ使っていない紹介割引は消える
        # 解約したときの追加FAXの残りは、支払い済み期間の終わり（＝契約が終わった今）で使えなくなる（返金なし、10/28本人決定）
        end_at = datetime.fromtimestamp(end, timezone.utc) if end else store.now()
        end_at = min(end_at, store.now())
        packs = [{**p, "exp": min(p["exp"], end_at)} for p in (u.get("fax_packs") or [])]
        store.update_user(uid, {"fax_packs": packs, "ended_at": store.now()})
    if plan == "free" and u.get("shop_code"):
        import campaign
        g = campaign.get_group(u["shop_code"])
        if g and g.get("status") == "active":
            campaign.apply_member(uid, g)  # 個人の契約が終わったので、店舗のプランに切り替える
    return uid, u.get("plan"), plan


def _ensure_sub_tax_ids(sub, active):
    """契約の請求書に登録番号を載せる（まだなら）。"""
    ids = account_tax_ids() if active else []
    have = [t if isinstance(t, str) else t["id"] for t in ((sub.get("invoice_settings") or {}).get("account_tax_ids") or [])]
    if ids and ids != have:
        try:
            _stripe().Subscription.modify(sub["id"], invoice_settings={"account_tax_ids": ids})
        except Exception:
            log.exception("登録番号を契約に付けられなかった %s", sub["id"])


def _track_yearly(uid, on):
    """年払いの方の一覧（更新30日前のお知らせ用）。"""
    def fn(m):
        ids = set(m.get("uids") or [])
        (ids.add if on else ids.discard)(uid)
        m["uids"] = sorted(ids)
    if store.db().get("meta", "yearly") is None:
        store.db().set("meta", "yearly", {"uids": []})
    store.db().update_tx("meta", "yearly", fn)


def annual_renew_notices():
    """年払いの更新日の30日前の方へのお知らせ [(uid, 差し込み)]。1回の更新につき1度だけ。"""
    out = []
    today = datetime.now(store.JST).date()
    for uid in (store.db().get("meta", "yearly") or {}).get("uids", []):
        u = store.get_user(uid)
        pe = (u or {}).get("period_end")
        if not u or u.get("billing_interval") != "year" or not pe or u.get("cancel_at_period_end"):
            continue
        if (pe.astimezone(store.JST).date() - today).days != 30 or u.get("annual_notified") == pe.isoformat():
            continue
        store.update_user(uid, {"annual_notified": pe.isoformat()})
        fee = {"light": "14,800円", "unlimited": "29,800円"}[u["plan"]]
        out.append((uid, {"プラン名": PLAN_NAMES[u["plan"]], "更新日": store.billing_renew_date(u), "料金": fee,
                          "年間件数": max(0, u.get("total_docs", 0) - u.get("year_base_docs", 0))}))
    return out


WEBHOOK_EVENTS = ["checkout.session.completed", "customer.subscription.created", "customer.subscription.updated",
                  "customer.subscription.deleted", "invoice.payment_failed", "invoice.paid",
                  "charge.refunded", "charge.dispute.created"]


def webhook_secret():
    """STRIPE_WEBHOOK_SECRET があればそれ、なければ ensure_webhook で作って Firestore にしまったもの。"""
    if config.STRIPE_WEBHOOK_SECRET:
        return config.STRIPE_WEBHOOK_SECRET
    return (store.db().get("meta", "stripe_webhook") or {}).get("secret")


def ensure_webhook():
    """Stripe の通知先（BASE_URL/stripe/webhook）がなければ作る。署名用の秘密は人の手を通さず Firestore にしまう。"""
    url = f"{config.BASE_URL}/stripe/webhook"
    if not (config.STRIPE_SECRET_KEY and url.startswith("https://") and "example.invalid" not in url):
        return
    if config.STRIPE_WEBHOOK_SECRET:
        return
    s = _stripe()
    doc = store.db().get("meta", "stripe_webhook") or {}
    if doc.get("id"):
        try:
            # キーを別のStripeアカウントのものに替えたときは見つからないので、作り直す
            ep = plain(s.WebhookEndpoint.retrieve(doc["id"]))
            if ep.get("url") == url:
                if set(ep.get("enabled_events") or []) != set(WEBHOOK_EVENTS):
                    s.WebhookEndpoint.modify(doc["id"], enabled_events=WEBHOOK_EVENTS)  # 受け取る通知を増やしたとき
                return
        except s.InvalidRequestError:
            pass
    # 複数のインスタンスが同時に作らないよう、先に場所を取る
    # （失敗しても1時間たてばやり直せるよう、時間ごとの名前にする）
    lock = "stripe_webhook_lock_" + store.now().strftime("%Y%m%d%H") + (doc.get("id") or "")
    if not store.db().create("meta", lock, {"at": store.now()}):
        return
    ep = plain(s.WebhookEndpoint.create(url=url, enabled_events=WEBHOOK_EVENTS, description="1分資料 サーバー"))
    store.db().set("meta", "stripe_webhook", {"id": ep["id"], "url": url, "secret": ep["secret"], "at": store.now()})
    log.info("Stripe の通知先を作りました %s", ep["id"])


GRACE_DAYS = 7


def _invoice_sub(inv):
    return inv.get("subscription") or ((inv.get("parent") or {}).get("subscription_details") or {}).get("subscription")


def end_grace():
    """定期実行から。お支払いの猶予（7日）が過ぎても払われていない契約を終える。終えた件数を返す。"""
    s = _stripe()
    n = 0
    for uid, u in store.db().before("users", "grace_until", store.now()):
        if not u.get("grace_until"):
            continue
        sid = u.get("stripe_subscription")
        if sid and u.get("sub_status") in ("past_due", "unpaid", "incomplete"):
            try:
                s.Subscription.cancel(sid)  # customer.subscription.deleted の通知で無料に戻る
                n += 1
                log.info("猶予切れで契約を終了 %s %s", uid, sid)
            except Exception:
                log.exception("猶予切れの終了に失敗 %s", uid)
                continue
        store.update_user(uid, {"grace_until": None})
    return n


def handle_webhook(payload: bytes, sig: str):
    """Stripeからの通知。戻り値は LINE で知らせる内容 [(uid, 文面ID, 差し込み)]。"""
    s = _stripe()
    ev = plain(s.Webhook.construct_event(payload, sig, webhook_secret()))
    if not store.first_event("stripe-" + ev["id"]):
        return []
    t, obj = ev["type"], ev["data"]["object"]
    notices = []
    md = obj.get("metadata") or {}
    if t == "checkout.session.completed" and md.get("purpose") == "group":
        import campaign
        notices += campaign.on_paid(obj)
    elif t.startswith("customer.subscription.") and md.get("purpose") == "group":
        import campaign
        notices += campaign.on_sub_event(t, ev, obj)
    elif t == "checkout.session.completed" and obj.get("mode") == "subscription":
        uid = obj.get("client_reference_id")
        store.update_user(uid, {"stripe_customer": obj["customer"]})
        sub = s.Subscription.retrieve(obj["subscription"])
        uid, before, after = sync_subscription(sub)
        if uid:
            notices.append((uid, "plan_started", {}))
        if uid and md.get("referral"):
            import referral
            referral.on_checkout(uid, obj)
        if uid and md.get("newbie") and after in ("light", "unlimited"):
            import campaign
            full = campaign.NORMAL_YEN[after]
            notices.append((uid, "shinjin_started", {"半額": f"{full // 2:,}", "通常料金": f"{full:,}",
                                                     "更新日": store.billing_renew_date(store.get_user(uid))}))
    elif t == "checkout.session.completed" and (obj.get("metadata") or {}).get("purpose") == "fax_pack":
        import naiken
        uid = obj.get("client_reference_id")
        if uid and obj.get("payment_status") == "paid":
            naiken.add_pack(uid)
            exp = store.now() + timedelta(days=naiken.PACK_DAYS)
            d = exp.astimezone(store.JST)
            notices.append((uid, "addfax_done", {"有効期限": f"{d.month}月{d.day}日（{'月火水木金土日'[d.weekday()]}）"}))
    elif t == "checkout.session.completed" and (obj.get("metadata") or {}).get("purpose") == "faxrecv_pack":
        import faxopt
        uid = obj.get("client_reference_id")
        if uid and obj.get("payment_status") == "paid":
            faxopt.add_pack(uid)
            d = (store.now() + timedelta(days=faxopt.PACK_DAYS)).astimezone(store.JST)
            notices.append((uid, "recvfax_done", {"有効期限": f"{d.month}月{d.day}日（{'月火水木金土日'[d.weekday()]}）",
                                                  "受取FAX残": faxopt.left(store.get_user(uid))}))
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
        uid, u = store.db().find("users", "stripe_customer", obj["customer"])
        # 猶予は失敗した日から7日（10/28本人決定）。再請求のたびに届くので、お知らせは最初の1回だけ
        if uid and _invoice_sub(obj):
            if not (u or {}).get("grace_until"):
                until = store.now() + timedelta(days=GRACE_DAYS)
                store.update_user(uid, {"pay_failed_at": store.now(), "grace_until": until})
                d = until.astimezone(store.JST)
                notices.append((uid, "plan_payment_failed",
                                {"猶予日": f"{d.month}月{d.day}日（{'月火水木金土日'[d.weekday()]}）"}))
    elif t == "invoice.paid":
        sid = _invoice_sub(obj)
        if sid:
            sync_subscription(s.Subscription.retrieve(sid))
            uid, _ = store.db().find("users", "stripe_customer", obj["customer"])
            if uid:
                store.update_user(uid, {"pay_failed_at": None, "grace_until": None})
                import referral
                notices += referral.on_invoice_paid(uid, obj)
    elif t in ("charge.refunded", "charge.dispute.created"):
        import referral
        customer = obj.get("customer")
        if not customer and obj.get("charge"):
            customer = plain(s.Charge.retrieve(obj["charge"])).get("customer")
        if customer:
            referral.on_refund(customer)
    return notices
