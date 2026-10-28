"""紹介特典（10/28本人決定）。文面は messages.json の referral_*。

紹介した方・紹介された方とも、有料プランの請求を1回だけ割り引く（税込）。
  ライト月払い444円・使い放題月払い888円・ライト年払い4,800円・使い放題年払い9,800円
- 紹介された方：紹介コード（OT-xxxxx）をトークで送る。有料プランの支払い画面にクーポンを付ける
  （新人さん応援の半額のほうが大きいときは、そちらだけ。店舗まとめ割には付けない）
- 紹介した方：紹介された方の最初の支払い（invoice.paid）のあと、ご自身の定額契約にクーポンを付ける。
  1回の請求に1つまで、残りは ref_queue に入れて順番に。1年に12回まで、1か月に5件を超えたら保留
- アプリ版（Google Play）の方：割引の代わりに内見FAX+10回（3か月有効）
- 返金・支払い取り消し：まだ使っていない割引を外す。解約したら、使っていない割引は消える
記録は referrals/{紹介された方のuid}：code, referrer, referred, status（accepted/applied/established/void/hold）
"""
import logging
import random
import re
from datetime import timedelta

import config
import store

log = logging.getLogger("referral")

AMOUNT = {("light", False): 444, ("unlimited", False): 888, ("light", True): 4800, ("unlimited", True): 9800}
COUPON = {("light", False): "ref_light_m", ("unlimited", False): "ref_unlimited_m",
          ("light", True): "ref_light_y", ("unlimited", True): "ref_unlimited_y"}
YEAR_LIMIT = 12
MONTH_HOLD = 5
CODE_RE = re.compile(r"OT-?([A-Z0-9]{5})")
_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def link(code):
    return f"https://otpromo.com/r/{code}"


# ---------- コード ----------

def code_of(uid):
    """その方の紹介コード。なければ作る。"""
    u = store.get_user(uid) or {}
    if u.get("referral_code"):
        return u["referral_code"]
    for _ in range(50):
        code = "OT-" + "".join(random.choice(_CHARS) for _ in range(5))
        if code.startswith("OT-S") or store.db().find("users", "referral_code", code)[0]:
            continue  # 店舗コード（OT-S-）と紛れないようにする
        store.update_user(uid, {"referral_code": code})
        return code
    raise RuntimeError("紹介コードが作れませんでした")


def parse(text):
    m = CODE_RE.fullmatch((text or "").upper().replace(" ", ""))
    return "OT-" + m.group(1) if m else None


def accept(uid, code):
    """紹介コードを受け付ける。戻り値 ok / invalid / not_eligible / already。"""
    referrer, ru = store.db().find("users", "referral_code", code)
    if not referrer:
        return "invalid"
    u = store.get_user(uid) or {}
    if u.get("referred_by") == code:
        return "already"
    if referrer == uid or u.get("referred_by") or u.get("paid_before") or (u.get("plan") or "free") != "free" \
            or u.get("shop_code"):
        return "not_eligible"
    email = (u.get("delivery") or {}).get("email")
    if email and email == (ru.get("delivery") or {}).get("email"):
        return "not_eligible"
    store.update_user(uid, {"referred_by": code, "referrer_uid": referrer})
    store.db().set("referrals", uid, {"code": code, "referrer": referrer, "referred": uid,
                                      "status": "accepted", "at": store.now()})
    return "ok"


def eligible(u):
    """紹介された方で、まだ有料プランを申し込んでいない。"""
    return bool(u.get("referred_by")) and not u.get("paid_before") and (u.get("plan") or "free") == "free" \
        and not u.get("shop_code")


# ---------- Stripe ----------

def coupon(plan, yearly):
    """1回だけ○円引きのクーポン。なければ作る。"""
    import billing
    s = billing._stripe()
    cid = COUPON[(plan, yearly)]
    try:
        return billing.plain(s.Coupon.retrieve(cid))["id"]
    except s.InvalidRequestError:
        return billing.plain(s.Coupon.create(
            id=cid, name=f"紹介割引 {AMOUNT[(plan, yearly)]:,}円", amount_off=AMOUNT[(plan, yearly)], currency="jpy",
            duration="once", metadata={"service": "maisoku", "kind": "referral"}))["id"]


def checkout_discount(u, plan, yearly, newbie):
    """支払い画面に付けるクーポン。新人さん応援と重なったら大きいほう（月払いは半額のほう）だけ。"""
    if not eligible(u):
        return None
    if newbie and NORMAL_HALF.get(plan, 0) >= AMOUNT[(plan, yearly)]:
        return None
    return coupon(plan, yearly)


NORMAL_HALF = {"light": 740, "unlimited": 1490}


def _fingerprints(customer):
    """そのお客様のカードの識別番号（同じカードでの自作自演を見分ける）。"""
    import billing
    s = billing._stripe()
    try:
        pms = billing.plain(s.PaymentMethod.list(customer=customer, type="card", limit=10))["data"]
        return {(p.get("card") or {}).get("fingerprint") for p in pms} - {None}
    except Exception:
        log.exception("カードの確認に失敗 %s", customer)
        return set()


def on_checkout(uid, obj):
    """紹介された方が支払い画面を終えた。同じカードなら対象外にする。"""
    rec = store.db().get("referrals", uid)
    if not rec or rec["status"] != "accepted":
        return
    ru = store.get_user(rec["referrer"]) or {}
    mine = _fingerprints(obj.get("customer"))
    theirs = _fingerprints(ru.get("stripe_customer")) if ru.get("stripe_customer") else set()
    status = "void" if mine & theirs else "applied"
    store.db().set("referrals", uid, {"status": status, "applied_at": store.now()}, merge=True)
    if status == "void":
        log.info("紹介を対象外に（同じカード） %s → %s", rec["referrer"], uid)


def on_invoice_paid(uid, invoice):
    """請求の支払いが済んだ。戻り値は LINE のお知らせ [(uid, 文面ID, 差し込み)]。"""
    out = []
    rec = store.db().get("referrals", uid)
    if rec and rec["status"] in ("accepted", "applied") and (invoice.get("amount_paid") or 0) > 0:
        out += establish(uid, rec)
    out += _apply_queue(uid, used=_used_referral(invoice) or _consumed(uid))
    return out


def _consumed(uid):
    """付けておいた紹介割引（1回だけのクーポン）が、もう契約から外れているか＝使われたか。"""
    import billing
    u = store.get_user(uid) or {}
    if not (u.get("ref_attached") and u.get("stripe_subscription")):
        return False
    try:
        sub = billing.plain(billing._stripe().Subscription.retrieve(u["stripe_subscription"]))
    except Exception:
        log.exception("契約の確認に失敗 %s", uid)
        return False
    return not (sub.get("discounts") or sub.get("discount"))


def _used_referral(invoice):
    for d in invoice.get("discounts") or []:
        c = (d.get("coupon") or (d.get("source") or {}).get("coupon")) if isinstance(d, dict) else None
        cid = c.get("id") if isinstance(c, dict) else c
        if cid in COUPON.values():
            return True
    return False


def establish(referred, rec, app=False):
    """紹介が成立した（紹介された方の最初の支払い）。紹介した方へ特典。"""
    referrer = rec["referrer"]
    ru = store.get_user(referrer) or {}
    now = store.now()
    done = [x for x in (ru.get("ref_rewards") or []) if x["at"] > now - timedelta(days=365)]
    month = [x for x in done if x["at"].astimezone(store.JST).strftime("%Y%m") == now.astimezone(store.JST).strftime("%Y%m")]
    name = ((store.get_user(referred) or {}).get("company") or {}).get("会社名")
    if len(done) >= YEAR_LIMIT:
        store.db().set("referrals", referred, {"status": "limit", "established_at": now}, merge=True)
        return [(referrer, "referral_limit", {})]
    if len(month) >= MONTH_HOLD:
        store.db().set("referrals", referred, {"status": "hold", "established_at": now}, merge=True)
        log.warning("紹介が1か月に%s件を超えたため保留 %s", MONTH_HOLD, referrer)
        return [(referrer, "referral_hold", {})]
    store.db().set("referrals", referred, {"status": "established", "established_at": now}, merge=True)

    def fn(u):
        u["ref_rewards"] = [x for x in (u.get("ref_rewards") or []) if x["at"] > now - timedelta(days=365)] + [
            {"referred": referred, "at": now}]
        u["ref_rewarded"] = u.get("ref_rewarded", 0) + 1
    store.db().update_tx("users", referrer, fn)
    if ru.get("plan_source") == "google_play" or (ru.get("firebase_uid") and not ru.get("line_uid")):
        # アプリ版の方は、割引の代わりに内見FAX+10回
        if (ru.get("plan") or "free") == "free":
            # 紹介した方がまだ無料体験中なら保留し、有料プランに入ったときに渡す（3か月の期限もそこから。10/28本人決定）
            store.db().update_tx("users", referrer, lambda u: u.__setitem__(
                "ref_app_pending", (u.get("ref_app_pending") or 0) + 1))
            return []
        import naiken
        naiken.add_pack(referrer)
        d = (now + timedelta(days=naiken.PACK_DAYS)).astimezone(store.JST)
        return [(referrer, "referral_app", {"期限": f"{d.month}月{d.day}日（{'月火水木金土日'[d.weekday()]}）"})]
    store.db().update_tx("users", referrer, lambda u: u.__setitem__(
        "ref_queue", (u.get("ref_queue") or []) + [{"referred": referred, "name": name or "", "at": now}]))
    return _apply_queue(referrer, joined=True)


def _apply_queue(uid, used=False, joined=False):
    """割引待ちの先頭を、次の請求に付ける（1回の請求に1つまで）。"""
    import billing
    u = store.get_user(uid) or {}
    q = list(u.get("ref_queue") or [])
    out = []
    if used:
        store.update_user(uid, {"ref_attached": None})
        u["ref_attached"] = None
    if not q or u.get("plan_source") != "stripe" or not u.get("stripe_subscription"):
        if joined and q:
            # お試し中などで定額契約がない方：有料プランをはじめてからの請求に付ける
            out.append((uid, "referral_joined", _joined_kw(u, q[-1], None)))
        return out
    plan, yearly = u.get("plan"), u.get("billing_interval") == "year"
    amount = AMOUNT.get((plan, yearly))
    if not amount:
        return out
    next_bill = u.get("period_end")
    if u.get("ref_attached"):
        # すでに次の請求に割引が付いているので、その次の請求に回す
        if joined:
            later = store._add_months(next_bill, 12 if yearly else 1) if next_bill else None
            out.append((uid, "referral_joined", _joined_kw(u, q[-1], later, wait=True)))
        return out
    s = billing._stripe()
    first = q.pop(0)
    s.Subscription.modify(u["stripe_subscription"], discounts=[{"coupon": coupon(plan, yearly)}])
    store.update_user(uid, {"ref_queue": q, "ref_attached": first["referred"]})
    if joined:
        out.append((uid, "referral_joined", _joined_kw(u, first, next_bill)))
    if used:
        out.insert(0, (uid, "referral_used", {"割引額": f"{amount:,}", "のこりの割引の案内":
                                              f"まだ使っていない紹介割引が{len(q) + 1}回分あります。次の請求から1回ずつ使います。"}))
    return out


def _joined_kw(u, item, when, wait=False):
    import texts
    plan, yearly = u.get("plan"), u.get("billing_interval") == "year"
    d = when.astimezone(store.JST) if when else None
    kw = {"紹介された方の会社名": item.get("name") or "",
          "割引を付ける請求日": f"{d.month}月{d.day}日" if d else "有料プランをはじめてから最初",
          "割引額": f"{AMOUNT.get((plan, yearly), 444):,}"}
    t = texts.raw("referral_joined", **kw)
    if not item.get("name"):
        t = t.replace("ご紹介いただいた様が", "ご紹介いただいた方が")
    if wait:
        t = t.split("\n\n")[0] + "\n\n" + (f"割引は1回の請求に1つまでのため、{kw['割引を付ける請求日']}の請求から"
                                          f"{kw['割引額']}円（税込）を割り引きます。")
    return {**kw, "text": t}


def on_refund(customer):
    """紹介された方に返金・支払い取り消しが出た。紹介した方のまだ使っていない割引を外す。"""
    import billing
    uid, _ = store.db().find("users", "stripe_customer", customer)
    rec = store.db().get("referrals", uid) if uid else None
    if not rec or rec["status"] not in ("established", "hold", "applied", "accepted"):
        return
    store.db().set("referrals", uid, {"status": "void", "voided_at": store.now()}, merge=True)
    referrer = rec["referrer"]
    ru = store.get_user(referrer) or {}
    store.update_user(referrer, {"ref_queue": [x for x in ru.get("ref_queue") or [] if x["referred"] != uid]})
    if ru.get("ref_attached") == uid and ru.get("stripe_subscription"):
        try:
            billing._stripe().Subscription.delete_discount(ru["stripe_subscription"])
        except Exception:
            log.exception("紹介割引を外せませんでした %s", referrer)
        store.update_user(referrer, {"ref_attached": None})
    log.info("紹介を取り消し %s → %s", referrer, uid)


def on_canceled(uid):
    """解約したら、まだ使っていない割引は消える。"""
    store.update_user(uid, {"ref_queue": [], "ref_attached": None})


# ---------- アプリ版 ----------

def on_play_paid(uid):
    """アプリ版で紹介された方が初めて有料プランを払った：両方に内見FAX+10回（紹介した方がLINE版なら割引）。"""
    rec = store.db().get("referrals", uid)
    if not rec or rec["status"] != "accepted":
        return []
    import naiken
    naiken.add_pack(uid)
    return establish(uid, rec, app=True)


def release_app_pending(uid):
    """アプリ版で、無料体験中に紹介して保留していた特典（内見FAX+10回）を、有料プランに入ったときに渡す。"""
    u = store.get_user(uid) or {}
    n = u.get("ref_app_pending") or 0
    if not n:
        return 0
    import naiken
    for _ in range(n):
        naiken.add_pack(uid)
    store.update_user(uid, {"ref_app_pending": 0})
    log.info("保留していた紹介特典を渡しました %s ×%s", uid, n)
    return n


def me(uid):
    u = store.get_user(uid) or {}
    return {"code": code_of(uid), "referred_by": u.get("referred_by"), "rewarded": u.get("ref_rewarded", 0),
            # 無料体験中に紹介して、有料プランに入ったら渡す特典の数（アプリ版：内見FAX+10回 × この数）
            "pending": u.get("ref_app_pending", 0)}


# ---------- LINE ----------

def menu_messages(uid):
    import texts
    code = code_of(uid)
    u = store.get_user(uid) or {}
    menu = texts.raw("referral_menu", 紹介コード=code)
    if (u.get("plan") or "free") == "free":
        menu = menu.replace("（ご自身のプランの金額です）。", "（ご自身のプランの金額です）。\n（お試し中の方は、有料プランをはじめてからの請求に付きます）")
    return [texts.build("referral_menu", text=menu, urls={}),
            texts.build("referral_forward", 紹介コード=code, 紹介リンク=link(code))]


def on_text(c, t):
    """紹介コードが送られてきたら受け付ける。扱ったら True。"""
    import texts
    code = parse(t)
    if not code:
        return False
    res = accept(c.uid, code)
    if res == "invalid":
        c.send(texts.build("referral_code_invalid"))
    elif res == "not_eligible":
        c.send(texts.build("referral_code_not_eligible"))
    else:
        msgs = [texts.build("referral_code_accepted")]
        if not c.u.get("registered"):
            msgs.append(texts.build("referral_reg_prompt"))
        c.send(*msgs)
    return True
