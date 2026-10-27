"""ご感想モニター（先着40社・ライト1か月無料・カード登録なし）。

- 申し込み：LINEで合言葉（MONITOR_CODE、既定「モニター」）→ Stripe でカードなしの無料期間つき契約を作る
  （無料期間が終わってカードがなければ自動で終わる：trial_settings.end_behavior.missing_payment_method=cancel）
- カード登録：Checkout（mode=setup）→ 契約の支払い方法に設定。有料の最初の期間だけ内見FAXが 20→30 回
- お知らせ：毎日の定期実行（/cron/daily）で、終了7日前・前日（カード未登録）・終了翌日（カード未登録）に push
"""
import calendar
import logging
from datetime import datetime, timedelta, timezone

import billing
import config
import line
import store
import texts

log = logging.getLogger("monitor")
CAPACITY = int(config.env("MONITOR_CAPACITY", "40"))
CODE = config.env("MONITOR_CODE", "モニター")
BONUS = int(config.env("MONITOR_BONUS", "10"))
WEEK = "月火水木金土日"


def _end_of_trial(now=None):
    """1か月後の同じ日の終わり（日本時間）。月末は丸める。"""
    n = (now or datetime.now(store.JST)).astimezone(store.JST)
    y, m = (n.year + (n.month == 12), n.month % 12 + 1)
    d = min(n.day, calendar.monthrange(y, m)[1])
    return datetime(y, m, d, 23, 59, 59, tzinfo=store.JST)


def date_label(dt):
    d = dt.astimezone(store.JST)
    return f"{d.month}月{d.day}日（{WEEK[d.weekday()]}）"


def _take_seat(uid):
    """先着の枠を1つ取る。取れたら True。"""
    def fn(meta):
        seats = meta.setdefault("seats", [])
        if uid in seats:
            return True
        if len(seats) >= CAPACITY:
            return False
        seats.append(uid)
        return True
    db = store.db()
    if db.get("meta", "monitor") is None:
        db.set("meta", "monitor", {"seats": []})
    return db.update_tx("meta", "monitor", fn)


def apply(uid):
    """モニターの申し込み。戻り値は文面ID。"""
    u = store.get_user(uid)
    if u.get("monitor"):
        return "monitor_accepted"
    if u.get("stripe_subscription") or u.get("stripe_customer"):
        return "monitor_full"  # 過去に有料の記録がある方は対象外（案内は「受付終了」と同じ文で）
    if not _take_seat(uid):
        return "monitor_full"
    end = _end_of_trial()
    s = billing._stripe()
    co = u.get("company") or {}
    cus = billing.plain(s.Customer.create(name=co.get("会社名") or None, metadata={"uid": uid}))
    sub = billing.plain(s.Subscription.create(
        customer=cus["id"],
        items=[{"price": config.PRICE_LIGHT, "tax_rates": billing.item_tax(config.PRICE_LIGHT)}],
        trial_end=int(end.timestamp()),
        trial_settings={"end_behavior": {"missing_payment_method": "cancel"}},
        payment_settings={"save_default_payment_method": "on_subscription"},
        metadata={"uid": uid, "monitor": "1"},
    ))
    store.update_user(uid, {"stripe_customer": cus["id"], "monitor": {
        "start": store.now(), "end": end.astimezone(timezone.utc), "card": False, "notified": [],
        "base_docs": u.get("total_docs", 0), "base_naiken": u.get("total_naiken", 0)}})
    billing.sync_subscription(sub)
    return "monitor_accepted"


def accepted_message(uid):
    u = store.get_user(uid)
    m = u.get("monitor") or {}
    end = m.get("end")
    return texts.build("monitor_accepted", 終了日=date_label(end) if end else "")


# ---------- カード登録 ----------

def card_url(uid):
    return f"{config.BASE_URL}/card?t={billing.sign(uid)}"


def card_checkout(uid):
    s = billing._stripe()
    u = store.get_user(uid)
    sess = s.checkout.Session.create(
        mode="setup", customer=u["stripe_customer"], currency="jpy", payment_method_types=["card"],
        client_reference_id=uid, metadata={"uid": uid, "purpose": "monitor_card"}, locale="ja",
        success_url=f"{config.BASE_URL}/paid?t={billing.sign(uid)}", cancel_url=f"{config.BASE_URL}/canceled")
    return sess.url


def on_setup_completed(obj):
    """カード登録（mode=setup）が済んだとき。契約の支払い方法にして、最初の有料期間の内見FAXを30回にする。"""
    s = billing._stripe()
    uid = obj.get("client_reference_id")
    u = store.get_user(uid) if uid else None
    if not u or not u.get("stripe_subscription"):
        return None
    si = billing.plain(s.SetupIntent.retrieve(obj["setup_intent"]))
    pm = si["payment_method"]
    s.Customer.modify(u["stripe_customer"], invoice_settings={"default_payment_method": pm})
    sub = billing.plain(s.Subscription.modify(u["stripe_subscription"], default_payment_method=pm))
    m = dict(u.get("monitor") or {})
    if not m:
        return None
    m["card"] = True
    trial_end = sub.get("trial_end")
    store.update_user(uid, {"monitor": m, "bonus_period": datetime.fromtimestamp(trial_end, timezone.utc) if trial_end else None})
    return (uid, "monitor_converted", {"終了日": date_label(m["end"])})


def bonus_sheets(u):
    """カード登録したモニターの、有料の最初の期間だけ足す内見FAXの回数（資料の枚数は増やさない）。"""
    bp, ps = u.get("bonus_period"), u.get("period_start")
    if bp and ps and abs((ps - bp).total_seconds()) < 3 * 86400 and (u.get("plan") == "light"):
        return BONUS
    return 0


# ---------- 毎日のお知らせ ----------

def counts(u):
    m = u.get("monitor") or {}
    return (u.get("total_docs", 0) - m.get("base_docs", 0), u.get("total_naiken", 0) - m.get("base_naiken", 0))


def seven_days_text(u, end):
    n, k = counts(u)
    t = texts.raw("monitor_7days", 件数=n, 内見件数=k, 終了日=date_label(end))
    first = t.split("\n")[1] if "\n" in t else ""
    if n == 0:
        t = t.replace(first, "この1か月、まだ資料をお作りしていません。よろしければ、お手元の募集図面を1枚送ってみてください。")
    elif k == 0:
        t = t.replace(f"（内見申込書FAX {k}件を含みます）", "")
    return t


def daily():
    """定期実行から1日1回呼ぶ。送った件数を返す。"""
    meta = store.db().get("meta", "monitor") or {}
    today = datetime.now(store.JST).date()
    sent = 0
    for uid in meta.get("seats", []):
        u = store.get_user(uid)
        m = (u or {}).get("monitor")
        if not m or not u.get("line_uid"):
            continue
        end = m["end"].astimezone(store.JST).date()
        left = (end - today).days
        done = set(m.get("notified") or [])
        urls = {"カードを登録して続ける": card_url(uid), "料金プランを見る": None}
        msg, key = None, None
        if left == 7 and "7days" not in done:
            key = "7days"
            msg = texts.build("monitor_7days", urls=urls, text=seven_days_text(u, m["end"]))
        elif left == 1 and "1day" not in done and not m.get("card"):
            key = "1day"
            msg = texts.build("monitor_1day", urls=urls, 終了日=date_label(m["end"]))
        elif left == -1 and "ended" not in done and not m.get("card"):
            key = "ended"
            msg = texts.build("monitor_ended")
        if msg:
            if line.push(u["line_uid"], [msg]):
                sent += 1
            m["notified"] = sorted(done | {key})
            store.update_user(uid, {"monitor": m})
    return sent
