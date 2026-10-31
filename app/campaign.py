"""キャンペーン：店舗まとめ割と新人さん応援。文面は messages.json の store_* / shinjin_*。

店舗まとめ割
  同じ店舗で3人以上。1人あたり ライト1,180円・使い放題2,480円（税込）、お申し込みから6か月間。
  受付は公開日（STORE_CAMPAIGN_START）から3か月。LINE版のみ。代表の方が人数分をまとめて払う（Stripeの数量課金）。
  店舗は groups/{店舗コード}。利用者には shop_code・shop_role（owner / member）・shop_name を持たせ、
  プラン・請求期間は店舗の契約から写す（plan_source="group"）。個人の契約が残っている方は、それが終わってから写す。
  7か月目からは通常の料金（1,480円・2,980円）に、日割りなしで切り替える。

新人さん応援
  NEWBIE_START〜NEWBIE_END（日本時間）に初めて有料プラン（月払い）を申し込んだ方は、最初の月が半額。
  Stripeは1回だけ50%引きのクーポンを自動で付ける。年払い・店舗まとめ割は対象外。
"""
import logging
import random
import re
from datetime import date, datetime, timedelta

import config
import line
import store
import texts

log = logging.getLogger("campaign")

GROUP_YEN = {"light": 1180, "unlimited": 2480}
NORMAL_YEN = {"light": 1480, "unlimited": 2980}
MIN_SEATS = 3
MAX_SEATS = 50
DISCOUNT_MONTHS = 6
ENDING_NOTICE_DAYS = 14
CODE_RE = re.compile(r"OT-?S-([A-Z0-9]{4})")
_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 0/O・1/I/L は読み違えやすいので使わない
WEEK = "月火水木金土日"


# ---------- 日付 ----------

def today():
    return datetime.now(store.JST).date()


def ymdw(d):
    """2027年4月30日（金）"""
    return f"{d.year}年{d.month}月{d.day}日（{WEEK[d.weekday()]}）"


def mdw(d):
    return f"{d.month}月{d.day}日（{WEEK[d.weekday()]}）"


def _date(s):
    return date.fromisoformat(s) if s else None


def _jst_date(dt):
    return dt.astimezone(store.JST).date() if isinstance(dt, datetime) else dt


def yen(n):
    return f"{n:,}"


# ---------- 期間 ----------

def store_window():
    """店舗まとめ割の受付期間 (初日, 最終日)。公開日が未設定なら (None, None)。"""
    start = _date(config.STORE_CAMPAIGN_START)
    if not start:
        return None, None
    return start, store._add_months(start, 3) - timedelta(days=1)


def store_open(d=None):
    start, end = store_window()
    if not start:
        return True  # 公開日を決める前（確認用）は受付中にしておく
    # 公開前（開始日より前）は確認用に受け付ける。案内の文には開始日〜終了日を出す
    return (d or today()) <= end


def store_window_text():
    start, end = store_window()
    if not start:
        return {"まとめ割受付開始日": "サービス公開日", "まとめ割受付終了日": "公開日から3か月後"}
    return {"まとめ割受付開始日": ymdw(start), "まとめ割受付終了日": ymdw(end)}


def newbie_active(d=None):
    d = d or today()
    return _date(config.NEWBIE_START) <= d <= _date(config.NEWBIE_END)


def newbie_eligible(u):
    """新人さん応援の対象か（期間中・有料プランを一度も申し込んでいない・店舗まとめ割でない）。"""
    return newbie_active() and not u.get("paid_before") and not u.get("shop_code") \
        and (u.get("plan") or "free") == "free"


def discount_until(start_d):
    """割引の最終日（お申し込み日から6か月間）。翌日から通常の料金。"""
    return store._add_months(start_d, DISCOUNT_MONTHS) - timedelta(days=1)


# ---------- 店舗 ----------

def get_group(code):
    return store.db().get("groups", code) if code else None


def new_code():
    for _ in range(50):
        code = "OT-S-" + "".join(random.choice(_CODE_CHARS) for _ in range(4))
        if not get_group(code):
            return code
    raise RuntimeError("店舗コードが作れませんでした")


def _track(code):
    def fn(m):
        m["codes"] = sorted(set(m.get("codes") or []) | {code})
    if store.db().get("meta", "groups") is None:
        store.db().set("meta", "groups", {"codes": []})
    store.db().update_tx("meta", "groups", fn)


def per_person(g):
    return (NORMAL_YEN if g.get("discount_ended") else GROUP_YEN)[g["plan"]]


def plan_label(plan):
    return {"light": "ライトプラン", "unlimited": "使い放題プラン"}[plan]


def money_kw(g, seats=None):
    seats = seats or g["seats"]
    end = _jst_date(g["discount_until"])
    return {"店舗名": g["name"], "店舗コード": g["code"], "人数": seats, "加入人数": len(g["members"]),
            "1人の料金": yen(per_person(g)), "合計": yen(per_person(g) * seats),
            "割引終了日": ymdw(end), "割引終了日の翌日": ymdw(end + timedelta(days=1)),
            "通常料金": yen(NORMAL_YEN[g["plan"]]), "通常合計": yen(NORMAL_YEN[g["plan"]] * seats),
            "プラン名": plan_label(g["plan"]),
            "次の請求日": mdw(_jst_date(g["period_end"])) if g.get("period_end") else ""}


def invite_kw(g):
    return {"店舗名": g["name"], "店舗コード": g["code"], "友だち追加URL": config.LINE_ADD_FRIEND_URL}


def _has_own_sub(u):
    """個人の有料プランの契約がまだ続いているか。"""
    return bool(u.get("stripe_subscription")) and u.get("plan_source") == "stripe"


def apply_member(uid, g):
    """店舗の契約内容を、メンバー1人のプランに写す。個人の契約が残っていれば、それが終わるまで待つ。"""
    u = store.get_user(uid)
    if not u:
        return
    data = {"shop_code": g["code"], "shop_name": g["name"],
            "shop_role": "owner" if uid == g["owner"] else "member", "paid_before": True}
    if not _has_own_sub(u) and g.get("status") == "active":
        data.update({"plan": g["plan"], "plan_source": "group", "billing_interval": "month",
                     "period_start": g.get("period_start"), "period_end": g.get("period_end"),
                     "sub_status": g.get("sub_status"), "cancel_at_period_end": bool(g.get("cancel_at_period_end"))})
    store.update_user(uid, data)


def _release(uid, keep_until=None):
    """店舗から外れた方。keep_until までは今のプランのまま（その後 daily で無料に戻す）。"""
    u = store.get_user(uid)
    if not u:
        return
    data = {"shop_code": None, "shop_role": None, "shop_name": None}
    if u.get("plan_source") == "group":
        if keep_until and keep_until > store.now():
            data.update({"plan_source": "group_leaving", "cancel_at_period_end": True})
        else:
            data.update({"plan": "free", "plan_source": None, "sub_status": None, "cancel_at_period_end": False})
    store.update_user(uid, data)


def sync_group(sub):
    """Stripeの店舗の契約（metadata.purpose=group）を groups とメンバーに写す。戻り値は代表の uid。"""
    import billing
    sub = billing.plain(sub)
    md = sub.get("metadata") or {}
    code, owner = md.get("code"), md.get("uid")
    if not code or not owner:
        log.warning("店舗コードのない店舗の契約 %s", sub["id"])
        return None
    it = sub["items"]["data"][0]
    start, end = billing._period(sub)
    begun = datetime.fromtimestamp(sub.get("start_date") or start, store.JST).date()
    plan, _ = billing.plan_of_price(it["price"]["id"])
    active = sub["status"] in ("active", "trialing", "past_due")
    if store.db().create("groups", code, {
            "code": code, "owner": owner, "name": md.get("shop", ""), "plan": plan or md.get("plan") or "light",
            "seats": it.get("quantity") or MIN_SEATS, "members": [owner], "names": {},
            "created_at": store.now(), "begun": datetime(begun.year, begun.month, begun.day, tzinfo=store.JST),
            "discount_until": datetime.combine(discount_until(begun), datetime.min.time(), store.JST),
            "discount_ended": False, "ending_notified": False, "leaving": [], "status": "active"}):
        _track(code)
    data = {"seats": it.get("quantity") or MIN_SEATS, "stripe_subscription": sub["id"],
            "stripe_customer": sub["customer"], "item": it["id"], "sub_status": sub["status"],
            "cancel_at_period_end": billing.canceling(sub),
            "period_start": datetime.fromtimestamp(start, store.JST) if start else None,
            "period_end": datetime.fromtimestamp(end, store.JST) if end else None,
            "status": "active" if active else "ended"}
    if plan:
        data["plan"] = plan
    store.db().set("groups", code, data, merge=True)
    g = get_group(code)
    store.update_user(owner, {"stripe_customer": sub["customer"]})
    for uid in g["members"]:
        if active:
            apply_member(uid, g)
        else:
            _release(uid)
    return owner


def checkout_url(uid):
    """代表の方の支払いページ（人数分）。"""
    import billing
    s = billing._stripe()
    u = store.get_user(uid)
    d = u.get("shop_draft") or {}
    if not (d.get("name") and d.get("plan") and (d.get("seats") or 0) >= MIN_SEATS):
        return None
    code = new_code()
    price = config.PRICE_GROUP_LIGHT if d["plan"] == "light" else config.PRICE_GROUP_UNLIMITED
    md = {"uid": uid, "purpose": "group", "code": code, "shop": d["name"][:40], "plan": d["plan"]}
    params = dict(mode="subscription", client_reference_id=uid, locale="ja",
                  custom_text={"submit": {"message": f"お申し込みにより、利用規約（{config.TERMS_URL}）に同意したものとします。"}},
                  line_items=[{"price": price, "quantity": d["seats"], "tax_rates": [billing.tax_rate(inclusive=True)]}],
                  subscription_data={"metadata": md}, metadata=md,
                  success_url=f"{config.BASE_URL}/paid?t={billing.sign(uid)}",
                  cancel_url=f"{config.BASE_URL}/canceled")
    if u.get("stripe_customer"):
        params["customer"] = u["stripe_customer"]
    elif (u.get("delivery") or {}).get("email"):
        params["customer_email"] = u["delivery"]["email"]
    return billing.plain(billing.create_checkout(s, **params))["url"]


def on_paid(obj):
    """店舗の支払いが済んだとき。戻り値は LINE のお知らせ [(uid, 文面ID, 差し込み)]。"""
    import billing
    s = billing._stripe()
    uid = obj.get("client_reference_id")
    store.update_user(uid, {"stripe_customer": obj["customer"], "shop_draft": None, "shop_step": None})
    owner = sync_group(s.Subscription.retrieve(obj["subscription"]))
    u = store.get_user(owner)
    # 代表の方の個人の契約は、更新日で終わらせる（そのあと店舗のプランに切り替わる）
    if _has_own_sub(u) and not u.get("cancel_at_period_end"):
        s.Subscription.modify(u["stripe_subscription"], cancel_at_period_end=True)
    g = get_group((obj.get("metadata") or {}).get("code"))
    if g:
        _remember_name(g["code"], owner)
    return [(owner, "store_paid", {})] if g else []


def _remember_name(code, uid):
    u = store.get_user(uid) or {}
    name = (u.get("company") or {}).get("担当者名") or line.profile(u.get("line_uid", "")).get("displayName") or "メンバー"
    _set_name(code, uid, name)
    return name


def _set_name(code, uid, name):
    def fn(d):
        d["names"] = {**(d.get("names") or {}), uid: name}
    store.db().update_tx("groups", code, fn)


def change_seats(g, seats):
    """人数を変える。次の請求から反映（日割りしない）。"""
    import billing
    s = billing._stripe()
    s.Subscription.modify(g["stripe_subscription"], proration_behavior="none",
                          items=[{"id": g["item"], "quantity": seats}])
    store.db().set("groups", g["code"], {"seats": seats}, merge=True)
    return get_group(g["code"])


def on_sub_event(t, ev, obj):
    """店舗の契約の変更・解約。"""
    import billing
    owner = sync_group(obj)
    g = get_group((obj.get("metadata") or {}).get("code"))
    prev = ev["data"].get("previous_attributes") or {}
    if g and t == "customer.subscription.updated" and billing.just_canceled(obj, prev):
        last = _jst_date(g["period_end"]) - timedelta(days=1)
        return [(uid, "store_canceled", {"店舗名": g["name"], "最終日": ymdw(last)}) for uid in g["members"]]
    return []


def daily():
    """毎朝：割引の終わりのお知らせ（14日前）、通常料金への切り替え（前日）、外れた方の期限。"""
    import billing
    out = []
    d = today()
    for code in (store.db().get("meta", "groups") or {}).get("codes", []):
        g = get_group(code)
        if not g:
            continue
        for x in list(g.get("leaving") or []):
            if x["until"] <= store.now():
                u = store.get_user(x["uid"])
                if u and u.get("plan_source") == "group_leaving":
                    store.update_user(x["uid"], {"plan": "free", "plan_source": None, "cancel_at_period_end": False})
                store.db().update_tx("groups", code, lambda m, x=x: m.__setitem__(
                    "leaving", [y for y in m.get("leaving") or [] if y["uid"] != x["uid"]]))
        if g.get("status") != "active" or g.get("discount_ended") or g.get("cancel_at_period_end"):
            continue
        left = (_jst_date(g["discount_until"]) - d).days
        if left <= ENDING_NOTICE_DAYS and not g.get("ending_notified"):
            store.db().set("groups", code, {"ending_notified": True}, merge=True)
            out.append((g["owner"], "store_discount_ending", money_kw(g)))
        if left <= 0:
            # 割引の最終日の朝に、次の請求から通常の料金になるよう価格を替える（日割りなし）
            s = billing._stripe()
            s.Subscription.modify(g["stripe_subscription"], proration_behavior="none",
                                  items=[{"id": g["item"], "price": billing.price_of(g["plan"]),
                                          "quantity": g["seats"], "tax_rates": [billing.tax_rate(inclusive=True)]}])
            store.db().set("groups", code, {"discount_ended": True}, merge=True)
            log.info("店舗まとめ割の期間終了 %s", code)
    return out


# ---------- LINE の会話 ----------

def _set(c, **kv):
    store.update_user(c.uid, kv)
    c.refresh()


def _params(v):
    act, _, rest = v.partition("&")
    return act, dict(x.partition("=")[::2] for x in rest.split("&") if x)


def plan_status_drop(u):
    """料金プランの状況から外すボタン。"""
    drop = []
    if u.get("shop_code") or not store_open():
        drop.append("postback store=start")
    if u.get("shop_role") != "owner":
        drop.append("postback store=members")
    return drop


def on_text(c, t):
    """店舗まとめ割の文字。扱ったら True。"""
    import bot
    n = bot.norm(t)
    m = CODE_RE.fullmatch(n.upper().replace(" ", ""))
    if m:
        join(c, "OT-S-" + m.group(1))
        return True
    if n in ("店舗まとめ割", "店舗まとめ"):
        on_postback(c, "start")
        return True
    if n == "メンバー管理":
        on_postback(c, "members")
        return True
    step = c.u.get("shop_step")
    if step == "name":
        d = dict(c.u.get("shop_draft") or {})
        d["name"] = n[:40]
        _set(c, shop_draft=d, shop_step="count")
        c.send(texts.build("store_ask_count"))
        return True
    if step == "count":
        num = re.sub(r"[人名\s]", "", n)
        if not num.isdigit():
            c.send(texts.build("store_ask_count"))
            return True
        _count(c, int(num))
        return True
    return False


def _count(c, n):
    if n < MIN_SEATS:
        return c.send(texts.build("store_count_too_few"))
    if n > MAX_SEATS:
        return c.send(line.text(f"{MAX_SEATS}人を超える場合は、「使い方・お問い合わせ」からご相談ください。"))
    d = dict(c.u.get("shop_draft") or {})
    d["seats"] = n
    _set(c, shop_draft=d, shop_step=None)
    end = discount_until(today())
    import billing
    c.send(texts.build("store_confirm", urls={"Stripe決済ページ（店舗まとめ割・人数分）":
                                              f"{config.BASE_URL}/pay/store?t={billing.sign(c.uid)}"},
                       店舗名=d["name"], プラン名=plan_label(d["plan"]), 人数=n,
                       **{"1人の料金": yen(GROUP_YEN[d["plan"]]), "合計": yen(GROUP_YEN[d["plan"]] * n),
                          "割引終了日": ymdw(end), "割引終了日の翌日": ymdw(end + timedelta(days=1)),
                          "通常料金": yen(NORMAL_YEN[d["plan"]])}))


def _owner_group(c):
    g = get_group(c.u.get("shop_code"))
    if not g or g.get("status") != "active":
        return None
    return g


def on_postback(c, v):
    import bot
    act, p = _params(v)
    if act == "start":
        g = get_group(c.u.get("shop_code"))
        if g and g.get("status") == "active":
            return on_postback(c, "members")
        if not store_open():
            return c.send(texts.build("store_closed", **store_window_text()))
        return c.send(texts.build("store_start", **store_window_text()))
    if act == "apply":
        if not store_open():
            return c.send(texts.build("store_closed", **store_window_text()))
        if not c.u.get("registered"):
            return c.send(line.text("店舗まとめ割のお申し込みの前に、会社情報のご登録をお願いします（2〜3分ほど）。",
                                    quick=[line.qr_postback("同意して登録をはじめる", "reg=start")]))
        if c.u.get("shop_code"):
            return on_postback(c, "members")
        d = c.u.get("shop_draft") or {}
        if d.get("plan") and d.get("name"):
            _set(c, shop_step="count")
            return c.send(texts.build("store_ask_count"))
        _set(c, shop_draft={}, shop_step=None)
        return c.send(texts.build("store_ask_plan"))
    if act == "plan":
        d = dict(c.u.get("shop_draft") or {})
        d["plan"] = "light" if p.get("p") in ("lite", "light") else "unlimited"
        _set(c, shop_draft=d, shop_step="name")
        return c.send(texts.build("store_ask_name"))
    if act == "count":
        return _count(c, int(p.get("n") or 0))
    if act == "cancel_flow":
        _set(c, shop_draft=None, shop_step=None)
        return c.send(line.text("かしこまりました。またいつでもお声がけください。"))
    if act == "join":
        return join(c, p.get("code", ""), confirmed=True)
    if act == "join_cancel":
        return c.send(line.text("かしこまりました。今のプランのまま、お使いください。"))

    # ここから先は、店舗に入っている方
    g = _owner_group(c)
    if not g:
        return on_postback(c, "start")
    if act == "invite":
        return c.send(texts.build("store_invite", **invite_kw(g)))
    if c.uid != g["owner"]:
        owner = store.get_user(g["owner"]) or {}
        name = (g.get("names") or {}).get(g["owner"]) or (owner.get("company") or {}).get("担当者名") or "代表の方"
        return c.send(texts.build("store_members_not_owner", 代表者名=name))
    if act == "members":
        return c.send(members_message(g))
    if act == "add":
        if not p.get("n"):
            return c.send(texts.build("store_add_ask", **money_kw(g)))
        g = change_seats(g, g["seats"] + max(1, min(int(p["n"]), MAX_SEATS - g["seats"])))
        return c.send(texts.build("store_count_changed", **money_kw(g)))
    if act == "reduce":
        empty = g["seats"] - len(g["members"])
        if g["seats"] - 1 < MIN_SEATS:
            return c.send(texts.build("store_reduce_too_few", 人数=MIN_SEATS))
        if not p.get("n"):
            if empty > 0:
                return c.send(texts.build("store_reduce_pick", 空き人数=empty))
            return on_postback(c, "remove")
        if empty <= 0:
            return on_postback(c, "remove")
        g = change_seats(g, g["seats"] - 1)
        return c.send(_reduced(g))
    if act == "remove":
        others = [u for u in g["members"] if u != g["owner"]]
        if not others:
            return c.send(line.text("外せるメンバーの方は、まだいません。"))
        if not p.get("m"):
            names = g.get("names") or {}
            btns = [{"label": names.get(u, "メンバー")[:20], "action": f"postback store=remove&m={u}"}
                    for u in others[:13]]
            return c.send(texts.build("store_remove_pick", buttons=btns))
        m = p["m"]
        if m not in others:
            return c.send(members_message(g))
        # メンバーは外せるが、請求は3人分を下回らない（10/28本人決定）
        new = max(MIN_SEATS, g["seats"] - 1)
        kw = money_kw(g, new)
        if g["seats"] - 1 >= MIN_SEATS:
            fee = f"人数は{new}人になり、次の請求から{kw['合計']}円（税込）になります。"
        else:
            fee = (f"お支払いは3人分からのため、料金は{kw['合計']}円（税込）のまま変わりません。"
                   "空いた枠には、あとから別の方を招待できます。")
        return c.send(texts.build("store_remove_confirm", メンバー名=(g.get("names") or {}).get(m, "メンバー"),
                                  メンバーID=m, 新しい人数=new, 料金の案内=fee,
                                  **{k: v for k, v in kw.items() if k != "人数"}))
    if act == "remove_ok":
        m = p.get("m")
        if m not in g["members"] or m == g["owner"]:
            return c.send(members_message(g))
        until = g.get("period_end") or store.now()

        def fn(d):
            d["members"] = [x for x in d["members"] if x != m]
            d["leaving"] = (d.get("leaving") or []) + [{"uid": m, "until": until}]
        store.db().update_tx("groups", g["code"], fn)
        _release(m, keep_until=until)
        g = get_group(g["code"])
        reduced = g["seats"] > MIN_SEATS
        if reduced:
            g = change_seats(g, g["seats"] - 1)
        mu = store.get_user(m) or {}
        if mu.get("line_uid"):
            line.push(mu["line_uid"], [texts.build("store_removed_member", 店舗名=g["name"],
                                                   最終日=ymdw(_jst_date(until) - timedelta(days=1)))])
        return c.send(_reduced(g) if reduced else members_message(g))
    return c.send(members_message(g))


def _reduced(g):
    kw = money_kw(g)
    first = texts.raw("store_count_changed", **kw).split("\n\n")[0]
    return texts.build("store_count_changed", text=first, buttons=[])


def members_message(g):
    names = g.get("names") or {}
    rows = []
    for uid in g["members"]:
        u = store.get_user(uid) or {}
        n = store.quota(u)["month_used"] if u else 0
        rows.append(f"・{names.get(uid, 'メンバー')}{'（代表）' if uid == g['owner'] else ''}／今月{n}件")
    empty = g["seats"] - len(g["members"])
    if empty > 0:
        rows.append(f"・（まだ加わっていません）{empty}人")
    kw = money_kw(g)
    text = texts.raw("store_members", メンバー一覧="\n".join(rows), **kw)
    if g.get("discount_ended"):
        text = re.sub(r"割引の期間：.*", f"割引の期間：{kw['割引終了日']}で終わりました", text)
    return texts.build("store_members", text=text)


# ---------- メンバーが加わる ----------

PERSONAL = ("担当者名", "ローマ字", "役職", "携帯", "メール", "名刺済み")


def join(c, code, confirmed=False):
    g = get_group(code)
    if not g or g.get("status") != "active":
        return c.send(texts.build("store_code_invalid"))
    if c.uid in g["members"]:
        return c.send(line.text(f"すでに{g['name']}のメンバーとしてお使いいただけます。"))
    if c.u.get("shop_code"):
        return c.send(line.text(f"いまは{c.u.get('shop_name') or 'ほかの店舗'}のメンバーになっています。"
                                "店舗を移る場合は、今の店舗の代表の方に「メンバーを外す」をお願いしてください。"))
    if len(g["members"]) >= g["seats"]:
        _notify_full(c, g)
        return c.send(texts.build("store_code_full", 店舗名=g["name"], 人数=g["seats"]))
    if _has_own_sub(c.u) and not c.u.get("cancel_at_period_end") and not confirmed:
        import billing
        return c.send(texts.build("store_code_already_paid", 店舗名=g["name"], 店舗コード=code,
                                  プラン名=billing.plan_name(c.u), 更新日=store.billing_renew_date(c.u)))

    def fn(d):
        if c.uid in d["members"] or len(d["members"]) >= d["seats"]:
            return False
        d["members"] = d["members"] + [c.uid]
        return True
    if not store.db().update_tx("groups", code, fn):
        g = get_group(code)
        if c.uid in g["members"]:
            return None
        _notify_full(c, g)
        return c.send(texts.build("store_code_full", 店舗名=g["name"], 人数=g["seats"]))
    if _has_own_sub(c.u) and not c.u.get("cancel_at_period_end"):
        import billing
        billing._stripe().Subscription.modify(c.u["stripe_subscription"], cancel_at_period_end=True)
    # 会社の情報は代表の方の登録をそのまま使う（名刺の情報だけ、ご本人のもの）
    owner = store.get_user(g["owner"]) or {}
    co = {k: v for k, v in (owner.get("company") or {}).items() if k not in PERSONAL}
    co.update({k: v for k, v in (c.u.get("company") or {}).items() if k in PERSONAL})
    co.pop("名刺済み", None)
    if (owner.get("company") or {}).get("FAX"):
        co["FAX"] = owner["company"]["FAX"]
    _set(c, company=co, registered=True, registered_at=store.now(), reg_queue=[], reg_mode=None,
         shop_draft=None, shop_step=None)
    g = get_group(code)
    apply_member(c.uid, g)
    c.refresh()
    import bot
    bot.link_menu(c.line_uid, True)
    name = _remember_name(code, c.uid)
    _notify_joined(g, name)
    bot.start_steps(c, ["staff", "mobile", "receive"], "member",
                    prefix=texts.build("store_code_accepted", 店舗名=g["name"]))


def _notify_full(c, g):
    owner = store.get_user(g["owner"]) or {}
    if owner.get("line_uid"):
        name = (c.u.get("company") or {}).get("担当者名") or line.profile(c.line_uid).get("displayName") or "メンバーの方"
        line.push(owner["line_uid"], [texts.build("store_full_notice", メンバー名=name, 店舗名=g["name"],
                                                  人数=g["seats"], **{"1人の料金": yen(per_person(g))})])


def _notify_joined(g, name):
    owner = store.get_user(g["owner"]) or {}
    if not owner.get("line_uid"):
        return
    mid = "store_all_joined" if len(g["members"]) >= g["seats"] else "store_member_joined"
    line.push(owner["line_uid"], [texts.build(mid, メンバー名=name, 店舗名=g["name"], 人数=g["seats"],
                                              加入人数=len(g["members"]))])


def member_done(c):
    import billing
    import naiken
    g = get_group(c.u.get("shop_code")) or {}
    plan = c.u.get("plan") or g.get("plan") or "light"
    body = {"light": f"募集図面{config.LIGHT_SHEETS}件分・内見申込FAX月{naiken.LIMITS['light'][0]}回",
            "unlimited": f"件数の上限なし・内見申込FAX月{naiken.LIMITS['unlimited'][0]}回"}.get(plan, "")
    c.send(texts.build("store_member_welcome", 店舗名=g.get("name", ""), プラン名=billing.plan_name(c.u),
                       プランの内容=body, 更新日=store.renew_date(c.u)))
