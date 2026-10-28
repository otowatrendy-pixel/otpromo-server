"""LINEの会話の流れ（友だち追加・初回登録・マイソク受け取り・資料のお届け・プラン案内）。

返事はできるだけ応答（reply）で返す。資料づくりに50秒以上かかったら「混み合っています」を応答し、資料は push で届ける。
"""
import json
import logging
import re
import tempfile
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import billing
import campaign
import config
import core
import delivery
import faxopt
import line
import monitor
import naiken_bot
import store
import texts
from schema import Result

log = logging.getLogger("bot")
pool = ThreadPoolExecutor(max_workers=int(config.env("WORKERS", "4")))
SLOW_SECONDS = 50

# 合言葉 → お試しに足す枚数（例 PROMO_CODES="FAX15:10;FAXB15:10"）。FAX DM のリストごとに変えて反応を測る
PROMO = {k.strip().upper(): int(v) for k, _, v in (x.partition(":") for x in config.env("PROMO_CODES").split(";")) if v}
RICHMENU_REGISTERED = config.env("RICHMENU_REGISTERED")
RICHMENU_UNREGISTERED = config.env("RICHMENU_UNREGISTERED")

# 登録の質問の順番と、項目ごとの「変更」で聞く質問
REG_STEPS = ["company", "shop", "staff", "mobile", "license", "tel", "fax", "address", "fee", "color", "font", "obi", "receive"]
EDIT_STEPS = {"company": ["company", "shop"], "staff": ["staff", "mobile"], "mobile": ["mobile"], "license": ["license"], "tel": ["tel", "fax"],
              "address": ["address"], "fee": ["fee"], "color": ["color"], "font": ["font"],
              "obi": ["obi"], "recv": ["receive"]}
EDIT_NAMES = {"company": "会社名・店舗名", "staff": "担当者名・携帯", "mobile": "携帯番号", "license": "免許番号", "tel": "電話・FAX", "address": "住所",
              "fee": "仲介手数料", "color": "テーマ色", "font": "書体", "obi": "帯の画像", "recv": "受け取り方"}
ASK = {"company": "reg_company", "shop": "reg_shop", "staff": "reg_staff", "mobile": "reg_mobile", "license": "reg_license", "tel": "reg_tel",
       "fax": "reg_fax", "address": "reg_address", "fee": "reg_fee", "fee_other": "reg_fee_other",
       "color": "reg_color", "color_code": "reg_color_code", "font": "reg_font", "obi": "reg_obi",
       "receive": "reg_receive", "mail": "reg_mail", "faxto": "reg_faxto", "send_mail": "send_mail_ask"}
COLORS = ["青", "紺", "水色", "緑", "深緑", "赤", "えんじ", "オレンジ", "茶", "紫", "ピンク", "黒"]
FONTS = ["ゴシック", "明朝", "丸ゴシック", "UDゴシック"]
LICENSE_RE = re.compile(r"(知事|大臣)\s*[（(]\s*\d+\s*[）)]\s*第?\s*\d+\s*号?")


def norm(s):
    return unicodedata.normalize("NFKC", s or "").strip()


# ---------- Webhook の入口 ----------

def handle_events(body: dict):
    for ev in body.get("events", []):
        eid = ev.get("webhookEventId")
        if eid and not store.first_event("line-" + eid):
            continue
        pool.submit(_safe, ev)


def _safe(ev):
    try:
        handle(ev)
    except Exception:
        log.exception("イベント処理で例外 %s", ev.get("type"))


class Ctx:
    """1つのイベントへの返事。応答トークンは1回しか使えないので、使ったかどうかを覚えておく。"""

    def __init__(self, ev):
        self.ev = ev
        self.line_uid = ev["source"].get("userId")
        self.token = ev.get("replyToken")
        self.lock = threading.Lock()
        self.uid, self.u = store.user_by_line(self.line_uid)

    def send(self, *messages):
        with self.lock:
            tok, self.token = self.token, None
        return line.reply_or_push(tok, self.line_uid, list(messages))

    def refresh(self):
        self.u = store.get_user(self.uid)
        return self.u


def handle(ev):
    if ev["source"].get("type") != "user":
        return
    c = Ctx(ev)
    t = ev["type"]
    if t == "follow":
        return on_follow(c)
    if t == "unfollow":
        return store.update_user(c.uid, {"blocked": True, "blocked_at": store.now()})
    if c.u.get("registered") and t in ("message", "postback"):
        import richmenu
        rid = RICHMENU_REGISTERED or richmenu.menu_id(True)
        if rid and c.u.get("menu_id") != rid:  # メニューを作り直したあとも、登録済みの方には大きいメニューを付ける
            link_menu(c.line_uid, True)
            store.update_user(c.uid, {"menu_id": rid})
    if t == "postback" and ev["postback"]["data"] == "terms=agree":
        held = c.u.get("terms_held")
        store.update_user(c.uid, {"agreed_at": store.now(), "terms_version": config.TERMS_VERSION, "terms_held": None})
        if held:
            return handle({**held, "replyToken": ev.get("replyToken")})  # 止めていた処理（届いた募集図面など）を続ける
        return c.send(line.text("ありがとうございます。そのままお使いください。"))
    if t in ("postback", "message") and needs_terms(c.u):
        if not (t == "postback" and ev["postback"]["data"].startswith("help")):
            store.update_user(c.uid, {"terms_held": {k: ev[k] for k in ("type", "source", "message", "postback",
                                                                        "webhookEventId") if k in ev}})
            return c.send(terms_message())
    if t == "postback":
        return on_postback(c, ev["postback"]["data"])
    if t != "message":
        return
    m = ev["message"]
    if m["type"] == "text":
        return on_text(c, m["text"])
    if m["type"] == "image":
        return on_file(c, m["id"], is_image=True)
    if m["type"] == "file":
        if (m.get("fileName") or "").lower().endswith(".pdf"):
            return on_file(c, m["id"], is_image=False)
        return c.send(texts.build("err_unsupported_file"))
    c.send(texts.build("err_other"))


# ---------- 利用規約 ----------

def needs_terms(u):
    """登録済みの方が、前の版の規約に同意したままなら True（規約を改めたとき、次に使うときにもう一度同意をいただく）。"""
    return bool(u.get("registered") and u.get("terms_version") and u["terms_version"] != config.TERMS_VERSION)


def terms_message():
    if "terms_updated" in texts._MSG:
        y, m, d = map(int, config.TERMS_VERSION.split("-"))
        import datetime as _dt
        改定日 = f"{m}月{d}日（{'月火水木金土日'[_dt.date(y, m, d).weekday()]}）"
        return texts.build("terms_updated", 規約URL=config.TERMS_URL, 改定日=改定日)
    return texts.build("follow", text=(f"利用規約を改めました。お手数ですが、内容をご確認のうえ「同意して続ける」を押してください。\n{config.TERMS_URL}"),
                       buttons=[{"label": "同意して続ける", "action": "postback terms=agree"}])


# ---------- 友だち追加 ----------

def on_follow(c):
    store.update_user(c.uid, {"blocked": False, "blocked_at": None})
    if c.u.get("registered"):
        link_menu(c.line_uid, True)
        return c.send(texts.build("refollow", 会社名=c.u["company"].get("会社名", ""), のこり枚数の案内=remain_note(c.u)))
    link_menu(c.line_uid, False)
    name = line.profile(c.line_uid).get("displayName", "")
    c.send(texts.build("follow", 表示名=name))


def link_menu(line_uid, registered):
    import richmenu
    rid = (RICHMENU_REGISTERED if registered else RICHMENU_UNREGISTERED) or richmenu.menu_id(registered)
    if rid:
        try:
            import requests
            requests.post(f"{line.API}/user/{line_uid}/richmenu/{rid}", headers=line._h(), timeout=10)
        except Exception:
            log.warning("リッチメニューの切り替えに失敗")


# ---------- 登録 ----------

def start_steps(c, steps, mode, prefix=None):
    store.update_user(c.uid, {"reg_queue": steps, "reg_mode": mode, "name_retry": 0})
    c.refresh()
    ask(c, steps[0], prefix=prefix)


def ask(c, step, prefix=None):
    msgs = []
    if step == "color":
        msgs.append(line.image(f"{config.BASE_URL}/static/colors.png", f"{config.BASE_URL}/static/colors.png"))
    if step == "font":
        msgs.append(line.image(f"{config.BASE_URL}/static/fonts.png", f"{config.BASE_URL}/static/fonts.png"))
    msgs.append(texts.build(ASK[step]))
    if prefix:
        msgs.insert(0, prefix)
    c.send(*msgs)


def current_step(u):
    q = u.get("reg_queue") or []
    return q[0] if q else None


def advance(c, insert=None):
    """今の質問を終えて次へ。insert があれば、その質問を次に差し込む。"""
    q = list(c.u.get("reg_queue") or [])
    if q:
        q.pop(0)
    if insert:
        q.insert(0, insert)
    store.update_user(c.uid, {"reg_queue": q})
    c.refresh()
    if q:
        return ask(c, q[0])
    mode = c.u.get("reg_mode")
    if mode == "edit":
        store.update_user(c.uid, {"reg_mode": None})
        return c.send(texts.build("reg_edit_done", 項目=c.u.get("reg_edit_name", "登録内容")))
    if mode == "member":
        store.update_user(c.uid, {"reg_mode": None})
        c.refresh()
        return campaign.member_done(c)
    if mode == "send_mail":
        store.update_user(c.uid, {"reg_mode": None})
        return send_last(c, "mail")
    return c.send(confirm_message(c.u))


def confirm_message(u, mid="reg_confirm"):
    co = u.get("company") or {}
    d = u.get("delivery") or {}
    recv = {"line": "LINE", "email": f"メール（{d.get('email', '')}）", "fax": f"FAX（{d.get('fax', '')}）"}
    return texts.build(mid, 会社名=co.get("会社名", ""), 店舗名=co.get("店舗名") or "なし",
                       担当者名=co.get("担当者名", ""), 携帯番号=co.get("携帯", ""),
                       免許番号=co.get("免許", ""), 電話=co.get("電話", ""), FAX=co.get("FAX") or "なし",
                       住所=co.get("住所", ""), ヶ月=f"{co.get('賃貸_仲介手数料_ヶ月', 1.0):g}",
                       色=co.get("テーマ色", "青"), 書体=co.get("書体", "ゴシック"),
                       帯="登録した画像" if co.get("帯画像") else "自動で作る",
                       受け取り方=recv.get(d.get("method", "line"), "LINE"))


def set_company(c, **kv):
    co = dict(c.u.get("company") or {})
    co.update(kv)
    store.update_user(c.uid, {"company": co})
    c.refresh()


def set_delivery(c, **kv):
    d = dict(c.u.get("delivery") or {"method": "line"})
    d.update(kv)
    store.update_user(c.uid, {"delivery": d})
    c.refresh()


def reg_text(c, step, t):
    """登録中に文字が送られてきたとき。"""
    t = norm(t)
    if step == "company":
        set_company(c, 会社名=t[:60])
    elif step == "shop":
        set_company(c, 店舗名="" if t in ("なし", "無し", "ない") else t[:40])
    elif step == "staff":
        import person
        if person.try_name(c.uid, c.u, t) == "retry":
            return c.send(texts.build("reg_staff_retry"))
        c.refresh()
        if c.u.get("shop_code"):
            campaign._set_name(c.u["shop_code"], c.uid, c.u["company"]["担当者名"])
    elif step == "mobile":
        import person
        m = person.normalize_mobile(t)
        if not m:
            return c.send(texts.build("reg_mobile_retry"))
        set_company(c, 携帯=m)
    elif step == "license":
        if not LICENSE_RE.search(t):
            store.update_user(c.uid, {"reg_license_draft": t[:60]})
            return c.send(texts.build("reg_license_retry"))
        set_company(c, 免許=t[:60])
    elif step == "tel":
        set_company(c, 電話=t[:20])
    elif step == "fax":
        set_company(c, FAX="" if t in ("なし", "無し", "ない") else t[:20])
    elif step == "address":
        set_company(c, 住所=t[:100])
    elif step in ("fee", "fee_other"):
        try:
            v = float(t.replace("ヶ月", "").replace("ヵ月", "").replace("か月", ""))
            assert 0 <= v <= 2
        except (ValueError, AssertionError):
            return c.send(texts.build("reg_fee_other"))
        set_company(c, 賃貸_仲介手数料_ヶ月=v)
    elif step in ("color", "color_code"):
        if t in COLORS:
            set_company(c, テーマ色=t)
        elif re.fullmatch(r"#?[0-9A-Fa-f]{6}", t):
            set_company(c, テーマ色="#" + t.lstrip("#").upper())
        else:
            return ask(c, "color_code")
    elif step == "font":
        if t not in FONTS:
            return ask(c, "font")
        set_company(c, 書体=t)
    elif step in ("mail", "send_mail"):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", t):
            return c.send(texts.build(ASK[step]))
        set_delivery(c, email=t, **({"method": "email"} if step == "mail" else {}))
    elif step == "faxto":
        if not re.fullmatch(r"[0-9\-]{9,15}", t):
            return c.send(texts.build("reg_faxto"))
        set_delivery(c, fax=t, method="fax")
    elif step in ("obi", "receive"):
        return ask(c, step)
    advance(c)


def reg_postback(c, k, v):
    step = current_step(c.u)
    if k == "shop" and step == "shop":
        set_company(c, 店舗名="")
    elif k == "license" and step == "license":
        set_company(c, 免許=c.u.get("reg_license_draft", ""))
    elif k == "fax" and step == "fax":
        set_company(c, FAX="")
    elif k == "fee" and step == "fee":
        if v == "other":
            return advance(c, insert="fee_other")
        set_company(c, 賃貸_仲介手数料_ヶ月=float(v))
    elif k == "color" and step == "color":
        if v == "code":
            return advance(c, insert="color_code")
        set_company(c, テーマ色=v)
    elif k == "font" and step == "font":
        set_company(c, 書体=v)
    elif k == "obi" and step == "obi":
        set_company(c, 帯画像=None)
    elif k == "recv" and step == "receive":
        if v == "mail":
            return advance(c, insert="mail")
        if v == "fax":
            return advance(c, insert="faxto")
        set_delivery(c, method="line")
    else:
        # 古いボタンが押されたときは、今の質問を聞き直す
        return ask(c, step) if step else None
    advance(c)


# ---------- ボタン（postback） ----------

def on_postback(c, data):
    k, _, v = data.partition("=")
    if k == "reg" and v == "start":
        store.update_user(c.uid, {"agreed_at": store.now(), "terms_version": config.TERMS_VERSION})
        return start_steps(c, list(REG_STEPS), "new")
    if k == "reg" and v == "done":
        if not c.u.get("company", {}).get("会社名"):
            return start_steps(c, list(REG_STEPS), "new")
        store.update_user(c.uid, {"registered": True, "reg_queue": [], "reg_mode": None, "registered_at": store.now()})
        link_menu(c.line_uid, True)
        return c.send(texts.build("reg_done"))
    if k == "reg" and v == "edit":
        store.update_user(c.uid, {"reg_queue": [], "reg_mode": None})
        return c.send(texts.build("reg_edit"))
    if k == "reg" and v == "show":
        return c.send(confirm_message(c.u))
    if k == "edit" and v == "staff" and c.u.get("registered"):
        import person
        until = person.change_wait(c.u)
        if until:
            d = until.astimezone(store.JST)
            return c.send(texts.build("reg_staff_locked",
                                      変更できる日時=f"{d.month}月{d.day}日（{'月火水木金土日'[d.weekday()]}）{d.hour}時"))
    if k == "edit" and v in EDIT_STEPS:
        store.update_user(c.uid, {"reg_edit_name": EDIT_NAMES[v]})
        mode = "edit" if c.u.get("registered") else "new"
        return start_steps(c, list(EDIT_STEPS[v]), mode)
    if k == "recv" and v == "line" and not current_step(c.u):
        # 回数券を使い切ったときの「LINEで受け取りにする」
        set_delivery(c, method="line")
        store.update_user(c.uid, {"recvfax_wait": None})
        return c.send(line.text("承知しました。これからは資料をLINEでお届けします。"))
    if k in ("shop", "license", "fax", "fee", "color", "font", "obi", "recv"):
        return reg_postback(c, k, v)
    if k == "help":
        return c.send(texts.build({"usage": "help_usage", "contact": "help_contact", "share": "help_share",
                                   "menu": "help_menu"}.get(v, "help_usage")))
    if k == "plan" and v == "show":
        return c.send(*plan_show(c))
    if k == "plan" and v == "status":
        return c.send(plan_status(c))
    if k == "send":
        return send_last(c, v)
    if k == "store":
        return campaign.on_postback(c, v)
    if k == "referral":
        import referral
        return c.send(*referral.menu_messages(c.uid))
    if k == "recvfax":
        return c.send(texts.build("recvfax_show", urls=plan_urls(c)))
    if k == "commercial" and v == "go":
        return pool.submit(_safe_pending, c, "commercial")
    if k == "addfax":
        return c.send(texts.build("addfax_show", urls=plan_urls(c)))
    if k == "naiken":
        return naiken_bot.on_postback(c, v)
    if k in ("card_roman", "card_role", "card_contact"):
        return naiken_bot.card_postback(c, k)
    if k == "edit" and v == "card":
        return naiken_bot.edit_card(c)
    if k == "cancel":
        if naiken_bot.active(c.u):
            return naiken_bot.cancel(c)
        store.update_user(c.uid, {"pending": None})
        return c.send(line.text("かしこまりました。またいつでもお送りください。"))


def plan_urls(c):
    return {"ライトを申し込む": billing.pay_url(c.uid, "light"),
            "Stripe決済ページ（ライト月額）": billing.pay_url(c.uid, "light"),
            "Stripe決済ページ（ライト年額）": billing.pay_url(c.uid, "light_year"),
            "Stripe決済ページ（使い放題月額）": billing.pay_url(c.uid, "unlimited"),
            "Stripe決済ページ（使い放題年額）": billing.pay_url(c.uid, "unlimited_year"),
            "使い放題を申し込む": billing.pay_url(c.uid, "unlimited"),
            "使い放題に切り替える": billing.pay_url(c.uid, "unlimited"),
            "変更・解約": billing.portal_url(c.uid),
            "お支払い情報を確認": billing.portal_url(c.uid),
            "10回追加する（500円）": billing.pay_url(c.uid, "faxpack"),
            # 文面の uri の行き先名でも引けるようにする
            "Stripe決済ページ（ライト）": billing.pay_url(c.uid, "light"),
            "Stripe決済ページ（使い放題）": billing.pay_url(c.uid, "unlimited"),
            "Stripe決済ページ（追加FAX）": billing.pay_url(c.uid, "faxpack"),
            "Stripe決済ページ（受け取り用FAX回数券）": billing.pay_url(c.uid, "faxrecv"),
            "Stripeのお客様ページ": billing.portal_url(c.uid),
            "Stripeのカード登録ページ": f"{config.BASE_URL}/card?t={billing.sign(c.uid)}"}


def plan_show(c):
    """料金プランの案内。新人さん応援の期間中は、初めての方に案内を先に付ける（同じ応答で1通分）。"""
    msgs = [texts.build("plan_show", urls=plan_urls(c))]
    import referral
    if campaign.newbie_eligible(c.u):
        msgs.insert(0, texts.build("shinjin_plan_note"))
    elif referral.eligible(c.u):
        msgs.insert(0, texts.build("referral_plan_note"))
    return msgs


def plan_status(c):
    import naiken
    q = store.quota(c.u)
    d = c.u.get("delivery") or {}
    month, packs, _ = naiken.remaining(c.u)
    live_packs = sorted([p for p in (c.u.get("fax_packs") or []) if p["exp"] > store.now() and p["used"] < p["n"]],
                        key=lambda p: p["exp"])
    text = texts.raw("plan_status", プラン名=billing.plan_name(c.u),
                     n="上限なし" if q["remaining"] is None else q["remaining"],
                     失効の案内="",
                     内見残=month, 追加FAXの案内=f"＋追加分{packs}回（{_md(live_packs[0]['exp'])}まで）" if packs else "",
                     更新日=store.renew_date(c.u),
                     受取FAX残=faxopt.left(c.u),
                     受取FAXの期限=f"（{_md(faxopt.next_expiry(c.u))}まで）" if faxopt.left(c.u) else "",
                     受け取り方={"line": "LINE", "email": "メール", "fax": "FAX"}.get(d.get("method"), "LINE"))
    if q["plan"] == "unlimited":
        text = re.sub(r"資料のこり：今月上限なし(枚|件分?)", f"資料：件数の上限なし（今月{q['month_used']}件）", text)
    elif q["plan"] == "free":
        text = text.replace("資料のこり：今月", "資料のこり：")  # お試しの枚数は月ごとではない
    return texts.build("plan_status", urls=plan_urls(c), text=text, drop=campaign.plan_status_drop(c.u))


# ---------- 文字 ----------

def on_text(c, t):
    if campaign.on_text(c, t):
        return
    import referral
    if referral.on_text(c, t):
        return
    step = current_step(c.u)
    if step:
        return reg_text(c, step, t)
    if naiken_bot.active(c.u) or norm(t).startswith("内見"):
        if not c.u.get("registered"):
            return c.send(texts.build("reg_first"))
        if norm(t).startswith("内見") or not naiken_bot.active(c.u):
            store.update_user(c.uid, {"nk": {}, "nk_step": "free", "nk_fid": None})
            c.refresh()
        return naiken_bot.on_text(c, t)
    # リッチメニューが使えないときも、文字で登録内容の変更を開けるように
    if c.u.get("registered") and norm(t) in ("変更", "登録内容の変更", "登録変更", "設定", "メニュー"):
        return on_postback(c, "reg=edit")
    if c.u.get("registered") and norm(t) in ("担当者", "担当者名", "担当者名の変更", "名前の変更", "携帯の変更"):
        return on_postback(c, "edit=staff")
    if norm(t) == monitor.CODE:
        if not c.u.get("registered"):
            return c.send(texts.build("reg_first"))
        mid = monitor.apply(c.uid)
        return c.send(monitor.accepted_message(c.uid) if mid == "monitor_accepted" else texts.build(mid))
    code = norm(t).upper()
    if code in PROMO:
        if store.apply_promo(c.uid, code, int(PROMO[code])):
            n = config.FREE_SHEETS + int(PROMO[code])
            return c.send(line.text(f"合言葉を受け付けました！\n無料でお試しいただけるのが、募集図面{n}件分になりました。"))
        return c.send(line.text("合言葉は、おひとり様1回までのご利用です。"))
    c.send(texts.build("err_text"))


# ---------- のこり枚数の案内 ----------

def _md(dt):
    d = dt.astimezone(store.JST)
    return f"{d.month}月{d.day}日（{'月火水木金土日'[d.weekday()]}）"


def remain_note(u):
    q = store.quota(u)
    if q["plan"] == "unlimited":
        if q["month_used"] == store.UNLIMITED_SOFT_CAP + 1:
            return texts.note("unlimited_over")
        return ""
    n = q["remaining"]
    if q["plan"] == "light":
        mid = "remain_light_low" if n <= 5 else "remain_light"
        return texts.note(mid, n=n, 更新日=store.renew_date(u))
    if n == 0:
        return texts.note("remain_free_zero")
    if n == 1:
        return texts.note("remain_free_last")
    return texts.note("remain_free", n=n)


def remain_buttons(u):
    q = store.quota(u)
    if q["plan"] == "free" and q["remaining"] <= 1:
        return [{"label": "料金プランを見る", "action": "postback plan=show"}]
    if q["plan"] == "light" and q["remaining"] <= 3:
        return [{"label": "料金プランを見る", "action": "postback plan=show"}]
    return []


# ---------- マイソクを受け取ったとき ----------

def on_file(c, message_id, is_image):
    step = current_step(c.u)
    if step == "obi" and is_image:
        data = line.content(message_id)
        try:
            img, _ = core.load_image(data)
        except core.InputError:
            return ask(c, "obi")
        store.db().put_file(f"users/{c.uid}/obi.png", core.render.png_bytes(img), "image/png")
        set_company(c, 帯画像=f"users/{c.uid}/obi.png")
        return advance(c)
    if not c.u.get("registered"):
        return c.send(texts.build("reg_first"))
    line.loading(c.line_uid, 60)
    data = line.content(message_id)
    process(c, data)


def company_for_render(uid, u):
    """帯画像は保存先から一時ファイルに出して渡す。"""
    co = dict(u.get("company") or {})
    key = co.get("帯画像")
    if key:
        b = store.db().get_file(key)
        if b:
            f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            f.write(b)
            f.close()
            co["帯画像"] = f.name
        else:
            co["帯画像"] = None
    return co


def hold(c, data, kind, result=None, pages=0):
    """あとで作るために、送られたマイソクを預かる（24時間）。"""
    store.db().put_file(f"users/{c.uid}/pending.bin", data, "application/octet-stream")
    store.update_user(c.uid, {"pending": {"kind": kind, "at": store.now(), "pages": pages,
                                          "result": result}})


def process(c, data, held=None):
    """資料を作って届ける。held は預かっていた読み取り結果（事業用の確認後など）。"""
    q = store.quota(c.u)
    if q["remaining"] is not None and q["remaining"] <= 0:
        hold(c, data, "quota")
        mid = "plan_light_empty" if q["plan"] == "light" else "plan_needed"
        return c.send(texts.build(mid, urls=plan_urls(c), 更新日=store.renew_date(c.u)))

    jid, job = store.new_job(c.uid, "line")
    store.update_job(jid, {"status": "processing"})
    done = threading.Event()

    def slow():
        if not done.wait(SLOW_SECONDS):
            c.send(texts.build("deliver_slow"))
    threading.Thread(target=slow, daemon=True).start()

    consumed = False
    try:
        if held:
            img, _ = core.load_image(data)
            a = {"img": img, "pages": held.get("pages", 0), "result": Result.model_validate(held["result"]), "log": {}}
        else:
            a = core.analyze(data)
            v = core.verdict(a["result"])
            # 事業用の賃貸も、そのまま作る（お届けのときに一言添える）
            if v not in ("ok", "commercial"):
                done.set()
                store.update_job(jid, {"status": "failed", "error": v, "log": a["log"]})
                return c.send(texts.build({"blurry": "err_blurry", "not_maisoku": "err_not_maisoku",
                                           "sale": "err_sale"}[v]))
        if not store.try_consume(c.uid):
            done.set()
            store.update_job(jid, {"status": "failed", "error": "quota"})
            return c.send(texts.build("plan_needed", urls=plan_urls(c)))
        consumed = True
        out = core.render_documents(company_for_render(c.uid, c.u), a["img"], a["result"], a["pages"], a["log"])
        store.save_files(jid, out)
        store.update_job(jid, {"status": "done", "title": out["title"], "texts": out["texts"], "log": out["log"],
                               "pages": out["pages"], "result": out["result"],
                               "verdict": core.verdict(a["result"])})
        recent = [{"id": jid, "title": out["title"], "at": store.now()}] + [r for r in (c.u.get("recent_jobs") or []) if r["id"] != jid]
        store.update_user(c.uid, {"last_job": jid, "pending": None, "recent_jobs": recent[:12]})
        c.refresh()
        done.set()
        deliver(c, jid)
        log.info("作成 %s %s", jid, json.dumps(out["log"], ensure_ascii=False))
    except core.InputError as e:
        done.set()
        if consumed:
            store.refund(c.uid)
        store.update_job(jid, {"status": "failed", "error": str(e)})
        c.send(line.text(str(e) + "\n\n※今回の分は、件数に数えていません。"))
    except Exception as e:
        done.set()
        log.exception("資料づくりで例外")
        if consumed:
            store.refund(c.uid)
        store.update_job(jid, {"status": "failed", "error": repr(e)[:500]})
        c.send(texts.build("err_system"))


def _safe_pending(c, kind):
    try:
        run_pending(c, kind)
    except Exception:
        log.exception("預かり分の処理で例外")


def run_pending(c, kind=None):
    """預かっていたマイソクで資料を作る。"""
    p = c.u.get("pending")
    if not p or (kind and p.get("kind") != kind) or p["at"] < store.now() - timedelta(hours=24):
        if kind:
            c.send(line.text("お預かりしていた募集図面の期限が切れました。お手数ですが、もう一度送ってください。"))
        return
    data = store.db().get_file(f"users/{c.uid}/pending.bin")
    if not data:
        return
    line.loading(c.line_uid, 60)
    store.update_user(c.uid, {"pending": None})
    c.refresh()
    process(c, data, held=p if p.get("result") else None)


# ---------- お届け ----------

def deliver(c, jid):
    job = store.get_job(jid)
    u = c.u
    d = u.get("delivery") or {}
    method = d.get("method", "line")
    exp = job["expires_at"].astimezone(store.JST)
    extra = []
    if job["texts"].get("shaky") or any(q.get("要確認") for q in job["texts"].get("qa", [])):
        extra.append(texts.note("deliver_partial"))
    if (job.get("pages") or 0) > 1:
        extra.append(texts.note("err_file"))
    remain = remain_note(u)
    pdf_url = store.file_url(jid, job, "shiryou.pdf")
    img = line.image(store.file_url(jid, job, "obikae.jpg"), store.file_url(jid, job, "preview.jpg"))
    if job.get("verdict") == "commercial":
        # 事業用のときは、資料の前に一言（同じ応答なので通数は増えない）
        img = [texts.build("err_commercial"), img]
    remain = "\n\n".join(x for x in extra + [remain] if x)
    # 何枚か続けて送られても、各資料の「この物件で内見FAX」はその物件で始まるよう資料IDを入れる
    common = dict(物件名=job["title"], のこり枚数の案内=remain, extra_buttons=remain_buttons(u), 資料ID=jid)
    if method == "email" and d.get("email"):
        pdf = store.db().get_file(f"jobs/{jid}/shiryou.pdf")
        if delivery.send_mail(d["email"], job["title"], pdf, pdf_url):
            return c.send(*_as_list(img), texts.build("deliver_mail", メールアドレス=d["email"], **common))
    if method == "fax":
        how, n = recvfax_send(c.uid, u, jid, job)
        if how == "sent":
            add, btns = recvfax_low(c.uid, n)
            common["のこり枚数の案内"] = "\n\n".join(x for x in (remain, add) if x)
            common["extra_buttons"] = list(common["extra_buttons"] or []) + btns
            return c.send(*_as_list(img), texts.build("deliver_fax", FAX番号=d["fax"], **common))
        if how == "empty":
            # 回数券がないときはFAXを送らずLINEで届け、買えばこの資料もFAXで送る
            store.update_user(c.uid, {"recvfax_wait": jid})
            common["のこり枚数の案内"] = "\n\n".join(x for x in (remain, texts.raw("recvfax_empty")) if x)
            common["extra_buttons"] = list(common["extra_buttons"] or []) + texts.buttons_of("recvfax_empty")
            common["urls"] = plan_urls(c)
    c.send(*_as_list(img), texts.build("deliver_line", リンク=pdf_url, 有効期限=f"{exp.month}月{exp.day}日", **common))


def _as_list(x):
    return x if isinstance(x, list) else [x]


def recvfax_send(uid, u, jid, job):
    """受け取り用FAXで、資料の2枚（帯替え図面＋初期費用概算書）を御社のFAXへ送る。
    無料お試し中は回数券なしで送る。戻り値は (結果, のこり回数)。結果は "sent"・"empty"（回数券なし）・None（送れない）。"""
    number = (u.get("delivery") or {}).get("fax")
    if not (number and delivery.fax_ready()):
        return None, None
    company = company_for_render(uid, u)
    if (u.get("plan") or "free") == "free":
        ok = delivery.send_fax(number, faxopt.build_pdf(company, {**job, "_id": jid}))
        return ("sent", None) if ok else (None, None)
    if not faxopt.can_send(u):
        return "empty", None
    n = faxopt.send_job(uid, u, jid, job, company)
    return ("sent", n) if n is not None else (None, None)


def recvfax_low(uid, n):
    """のこりが少ないときの一文とボタン（recvfax_low）。"""
    if n is None or n > faxopt.LOW:
        return "", []
    exp = faxopt.next_expiry(store.get_user(uid))
    t = texts.raw("recvfax_low", 受取FAX残=n, 有効期限=_md(exp) if exp else "")
    if not exp:
        t = t.replace("（まで）", "")
    return t, texts.buttons_of("recvfax_low")


def send_last(c, how):
    """届いた資料を、別の送り先にも送る（「メールにも送る」など）。"""
    jid = c.u.get("last_job")
    job = store.get_job(jid) if jid else None
    if not job or job.get("status") != "done":
        return c.send(line.text("お送りできる資料が見つかりませんでした。募集図面を送ってください。"))
    d = c.u.get("delivery") or {}
    if how == "line":
        exp = job["expires_at"].astimezone(store.JST)
        return c.send(texts.build("deliver_line", 物件名=job["title"], リンク=store.file_url(jid, job, "shiryou.pdf"),
                                  有効期限=f"{exp.month}月{exp.day}日", のこり枚数の案内="", 資料ID=jid))
    if how == "mail":
        if not d.get("email"):
            store.update_user(c.uid, {"reg_queue": ["send_mail"], "reg_mode": "send_mail"})
            return c.send(texts.build("send_mail_ask"))
        pdf = store.db().get_file(f"jobs/{jid}/shiryou.pdf")
        if delivery.send_mail(d["email"], job["title"], pdf, store.file_url(jid, job, "shiryou.pdf")):
            return c.send(texts.build("send_mail_done", メールアドレス=d["email"]))
        return c.send(line.text("ただいまメールでのお届けを準備中です。もうしばらくお待ちください。"))
    if how == "fax":
        if not d.get("fax"):
            store.update_user(c.uid, {"reg_queue": ["faxto"], "reg_mode": "edit", "reg_edit_name": "FAXの送り先"})
            return c.send(texts.build("reg_faxto"))
        if (c.u.get("plan") or "free") != "free" and not faxopt.can_send(c.u):
            store.update_user(c.uid, {"recvfax_wait": jid})
            return c.send(texts.build("send_fax_option", urls=plan_urls(c)))
        res, n = recvfax_send(c.uid, c.u, jid, job)
        if res == "sent":
            return c.send(send_fax_done(c.uid, d["fax"], n))
        return c.send(line.text("ただいまFAXでのお届けを準備中です。もうしばらくお待ちください。"))


def send_fax_done(uid, number, n):
    msg = texts.build("send_fax_done", FAX番号=number, 受取FAX残=n if n is not None else "")
    if n is None:  # お試し中は回数券を使わない
        msg = texts.build("send_fax_done", FAX番号=number,
                          text=texts.raw("send_fax_done", FAX番号=number).split("\n受け取り用FAXは")[0])
    return msg


# ---------- Stripe からの通知を LINE で知らせる ----------

def notify(uid, mid, kw):
    u = store.get_user(uid)
    if not u or not u.get("line_uid"):
        return
    if mid == "plan_started":
        q = store.quota(u)
        import naiken
        body = {"light": f"毎月、募集図面{config.LIGHT_SHEETS}件分の資料と内見申込FAX{naiken.LIMITS['light'][0]}回をお使いいただけます。",
                "unlimited": f"資料は件数を気にせず、内見申込FAXは月{naiken.LIMITS['unlimited'][0]}回までお使いいただけます。"}.get(q["plan"], "")
        kw = {"プラン名": billing.plan_name(u), "プランの内容": body, "更新日": store.billing_renew_date(u)}
    elif mid == "plan_canceled":
        kw = {"期限": store.renew_date(u)}
    elif mid == "plan_payment_failed":
        kw = {"猶予日": "1週間後", "urls": {"お支払い情報を確認": billing.portal_url(uid)}}
    elif mid == "store_paid":
        g = campaign.get_group(u.get("shop_code")) or {}
        if not g:
            return
        line.push(u["line_uid"], [texts.build("store_paid", **campaign.money_kw(g)),
                                  texts.build("store_invite", **campaign.invite_kw(g))])
        return
    elif mid == "store_discount_ending":
        kw["urls"] = {"変更・解約": billing.portal_url(uid), "Stripeのお客様ページ": billing.portal_url(uid)}
    elif mid == "plan_annual_renew":
        kw["urls"] = {"変更・解約": billing.portal_url(uid), "Stripeのお客様ページ": billing.portal_url(uid)}
    urls = kw.pop("urls", None)
    line.push(u["line_uid"], [texts.build(mid, urls=urls, **kw)])
    if mid == "recvfax_done" and u.get("recvfax_wait"):
        pool.submit(_send_waiting_fax, uid)
    if mid == "plan_started" and u.get("pending"):
        c = Ctx({"source": {"userId": u["line_uid"]}})
        pool.submit(_safe_pending, c, None)


def _send_waiting_fax(uid):
    """回数券を買う前に待っていた資料を、FAXで送る。"""
    try:
        u = store.get_user(uid)
        jid = u.get("recvfax_wait")
        store.update_user(uid, {"recvfax_wait": None})
        job = store.get_job(jid) if jid else None
        if not job or job.get("status") != "done" or job.get("user_id") not in (None, uid):
            return
        res, n = recvfax_send(uid, u, jid, job)
        if res == "sent":
            line.push(u["line_uid"], [send_fax_done(uid, u["delivery"]["fax"], n)])
    except Exception:
        log.exception("waiting fax")
