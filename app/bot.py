"""LINEの会話の流れ（友だち追加・初回登録・マイソク受け取り・資料のお届け・プラン案内）。

返事はできるだけ応答（reply）で返す。資料づくりに50秒以上かかったら「混み合っています」を応答し、資料は push で届ける。
"""
import io
import json
import logging
import re
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import billing
import campaign
import config
import core
import costs
import delivery
import faxopt
import growth
import line
import monitor
import naiken_bot
import sale_bot
import store
import texts
import usage
from PIL import Image
import schema
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
              "obi": ["obi"], "recv": ["receive"], "sfee": ["sfee"], "assoc": ["assoc"]}
EDIT_NAMES = {"company": "会社名・店舗名", "staff": "担当者名・携帯", "mobile": "携帯番号", "license": "免許番号", "tel": "電話・FAX", "address": "住所",
              "fee": "仲介手数料", "color": "テーマ色", "font": "書体", "obi": "帯の画像", "recv": "受け取り方",
              "sfee": "売買の仲介手数料", "assoc": "所属団体"}
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
        messages = list(messages)
        if tok and self.u.get("held_pushes"):
            # 通数の上限で送れなかったお知らせを、応答（通数に数えない）と一緒に届ける
            messages, rest = usage.with_held(self.u, messages)
            store.update_user(self.uid, {"held_pushes": rest, "held_pushes_at": self.u.get("held_pushes_at") if rest else None})
            self.u = {**self.u, "held_pushes": rest}
        return line.reply_or_push(tok, self.line_uid, messages)

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
    if t in ("postback", "message"):
        o = usage.outage()
        if o:
            # 「障害中」の間は、決まった一言を応答で返す（通数に数えない）
            return c.send(texts.build("ops_outage_reply", **(o.get("values") or {})))
    if t in ("postback", "message") and c.u.get("recvfax_remind_at"):
        # 「このままFAXする」のお知らせは、その間にほかの操作があれば送らない
        store.update_user(c.uid, {"recvfax_remind_at": None})
    if t == "postback":
        data = ev["postback"]["data"]
        dt = (ev["postback"].get("params") or {}).get("datetime")
        if dt:  # 日時選択のボタン（内見FAXの送信予約）で選ばれた日時
            data += "&dt=" + dt
        return on_postback(c, data)
    if t != "message":
        return
    m = ev["message"]
    if m["type"] == "text":
        return on_text(c, m["text"])
    if m["type"] == "image":
        import youbou
        if youbou.on_image(c, m["id"]):  # ご要望・不具合の受付中に届いた画像（図面としては扱わない）
            return
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
    if sale_bot.SALE_ONLY:
        co = sale_bot.rental_company(c)
        if co:  # 賃貸版にも登録済みの方：会社情報を引き継げる
            return c.send(texts.build("sale_follow_copy", 表示名=name, 会社名=co.get("会社名", "")))
        return c.send(texts.build("sale_follow", 表示名=name))
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
    if step in sale_bot.ASK:  # 売買版の質問（使い方・所属団体・売買の仲介手数料）
        msgs = [sale_bot.ask_message(c, step)]
        if prefix:
            msgs.insert(0, prefix)
        return c.send(*msgs)
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
    if mode == "sale_setup":  # 売買の図面を預かって2つ聞いたあと
        return sale_bot.resume_after_setup(c)
    return c.send(confirm_message(c.u))


def confirm_message(u, mid="reg_confirm"):
    co = u.get("company") or {}
    d = u.get("delivery") or {}
    recv = {"line": "LINE", "email": f"メール（{d.get('email', '')}）", "fax": f"FAX（{d.get('fax', '')}）"}
    extra = {}
    if mid == "reg_confirm" and sale_bot.on() and (u.get("use_kind") in ("sale", "both") or "所属団体" in co):
        mid, extra = "sale_reg_confirm", sale_bot.confirm_fields(u)
    return texts.build(mid, **extra, 会社名=co.get("会社名", ""), 店舗名=co.get("店舗名") or "なし",
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
    if step in sale_bot.ASK:
        return _sale_step(c, step, sale_bot.reg_text(c, step, t))
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


def _sale_step(c, step, res):
    """売買版の質問の答えを受けて、次へ進む・差し込む・聞き直す・使い方に合わせて並べ直す。"""
    if res is None:
        return ask(c, step)
    what, arg = res
    if what == "ask":
        return ask(c, arg)
    if what == "rebuild":  # 「賃貸／売買／両方」の答えで、残りの質問を並べ直す
        store.update_user(c.uid, {"reg_queue": ["kind"] + sale_bot.reg_steps(REG_STEPS, arg)})
        c.refresh()
        return advance(c)
    return advance(c, insert=arg)


def reg_postback(c, k, v):
    step = current_step(c.u)
    if k in ("kind", "assoc", "sfee"):
        res = sale_bot.reg_postback(c, step, k, v) if step else None
        return _sale_step(c, step, res) if step else None
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
        if sale_bot.SALE_ONLY:  # 売買版のLINE：賃貸の仲介手数料は聞かず、住所の後に所属団体と売買の仲介手数料
            store.update_user(c.uid, {"use_kind": "sale"})
            return start_steps(c, sale_bot.reg_steps(REG_STEPS, "sale"), "new")
        return start_steps(c, list(REG_STEPS), "new")
    if k == "reg" and v == "copy" and sale_bot.SALE_ONLY:  # 賃貸版の会社情報を引き継ぐ
        store.update_user(c.uid, {"agreed_at": store.now(), "terms_version": config.TERMS_VERSION})
        if not sale_bot.copy_from_rental(c):
            store.update_user(c.uid, {"use_kind": "sale"})
            return start_steps(c, sale_bot.reg_steps(REG_STEPS, "sale"), "new",
                               prefix=line.text("賃貸版の登録内容が見つかりませんでした。お手数ですが、はじめから登録をお願いします。"))
        return start_steps(c, ["assoc", "sfee"], "new", prefix=texts.build("sale_copy_done", 会社名=c.u["company"].get("会社名", "")))
    if k == "reg" and v == "done":
        if not c.u.get("company", {}).get("会社名"):
            return start_steps(c, list(REG_STEPS), "new")
        store.update_user(c.uid, {"registered": True, "reg_queue": [], "reg_mode": None, "registered_at": store.now()})
        if sale_bot.SALE_ONLY:
            store.launch_trial(c.uid)  # 売買版の公開記念（期間中の登録はお試し10件・30日以内）
        growth.on_registered(c.uid)
        link_menu(c.line_uid, True)
        if sale_bot.on() and c.u.get("use_kind") in ("sale", "both"):
            return c.send(texts.build("sale_reg_done"))
        return c.send(texts.build("reg_done"))
    if k == "reg" and v == "edit":
        store.update_user(c.uid, {"reg_queue": [], "reg_mode": None})
        show = (c.u.get("company") or {}).get("携帯を載せる", True)
        toggle = {"label": "携帯を図面に載せない" if show else "携帯を図面に載せる",
                  "action": "postback edit=mobile_hide" if show else "postback edit=mobile_show"}
        # 「宛名を聞かない／聞く」は今の設定に合わせた名前で出す
        ask = asks_name(c.u)
        btns = [dict(b, label="宛名を聞かない" if ask else "宛名を毎回聞く") if b["action"] == "postback edit=atena" else b
                for b in texts.buttons_of("reg_edit")]
        if sale_bot.on():  # 「仲介手数料」を「売買の設定」に（ボタンの数は変えない）
            btns = [{"label": "売買の設定", "action": "postback edit=deal"} if b["action"] == "postback edit=fee" else b
                    for b in btns]
        return c.send(texts.build("reg_edit", buttons=btns, extra_buttons=[toggle]))
    if k == "edit" and v in ("mobile_hide", "mobile_show") and c.u.get("registered"):
        show = v == "mobile_show"
        set_company(c, 携帯を載せる=show)
        return c.send(line.text(
            "これから作る帯替え図面に、携帯番号を載せます。" if show else
            "これから作る帯替え図面には、携帯番号を載せません（「担当：お名前」だけ載ります）。\n"
            "内見申込書は管理会社への連絡用のため、携帯番号はこれまでどおり載ります。"))
    if k == "edit" and v in ("atena", "atena_off", "atena_on") and c.u.get("registered"):
        on = v == "atena_on" if v != "atena" else not asks_name(c.u)
        set_company(c, 宛名を聞く=on)
        return c.send(texts.build("atena_on_done" if on else "atena_off_done"))
    if k == "atena" and v == "skip":
        if c.u.get("name_wait"):
            return on_name(c, "不要")
        return c.send(line.text("宛名をお待ちしている資料はありません。募集図面を送っていただくと、宛名をお聞きします。"))
    if k == "reg" and v == "show":
        return c.send(confirm_message(c.u))
    if k == "edit" and v == "staff" and c.u.get("registered"):
        import person
        until = person.change_wait(c.u)
        if until:
            d = until.astimezone(store.JST)
            return c.send(texts.build("reg_staff_locked",
                                      変更できる日時=f"{d.month}月{d.day}日（{'月火水木金土日'[d.weekday()]}）{d.hour}時"))
    if sale_bot.on(c) and c.u.get("registered") and sale_bot.edit_postback(c, k, v):
        return
    if k == "edit" and v in EDIT_STEPS:
        store.update_user(c.uid, {"reg_edit_name": EDIT_NAMES[v]})
        mode = "edit" if c.u.get("registered") else "new"
        return start_steps(c, list(EDIT_STEPS[v]), mode)
    if k == "recv" and v == "line" and not current_step(c.u):
        # 回数券を使い切ったときの「LINEで受け取りにする」
        set_delivery(c, method="line")
        store.update_user(c.uid, {"recvfax_wait": None})
        return c.send(line.text("承知しました。これからは資料をLINEでお届けします。"))
    if k in ("shop", "license", "fax", "fee", "color", "font", "obi", "recv", "kind", "assoc", "sfee"):
        return reg_postback(c, k, v)
    if k == "help" and sale_bot.on(c) and v in ("menu", "sale"):
        return c.send(texts.build("sale_help_menu" if v == "menu" else "sale_help_usage"))
    if k == "help":
        return c.send(texts.build({"usage": "help_usage", "contact": "help_contact", "share": "help_share",
                                   "menu": "help_menu"}.get(v, "help_usage")))
    if k == "motofax" and v == "start":  # 売買版のメニュー「元付へFAX」：賃貸の内見申込書と売買の物件確認を振り分け
        if not (sale_bot.on(c) and sale_bot.fax_start(c)):
            return naiken_bot.on_postback(c, "start")
        return
    if k == "bfax" and sale_bot.on(c):
        # 売買の物件確認・内見依頼書（baibai_naiken.py）。宛先・希望日時・送信・予約・回数は賃貸の内見FAXの流れを使う
        return sale_bot.bfax_postback(c, v)
    if k == "lsim" and sale_bot.on(c):  # この資料だけ、住宅ローンの条件を変えて作り直す
        return sale_bot.lsim_ask(c, v.partition("doc=")[2] or c.u.get("last_job"))
    if k in ("lq", "lq2") and sale_bot.on(c):  # 図面の直後のローンの質問
        return sale_bot.lq_postback(c, k, v)
    if k == "eq" and sale_bot.on(c):  # 戸建て・土地：土地の評価額の出し方（①路線価図／②地価公示）
        return sale_bot.eq_postback(c, v)
    if k == "tq" and sale_bot.on(c):  # 800万円以下の物件の仲介手数料（図面の直後・届いた資料の「手数料を変える」）
        return sale_bot.tq_postback(c, v)
    if k == "hand" and sale_bot.on(c):  # この資料だけ、決済日（引渡日）を変えて作り直す
        jid = v.partition("doc=")[2] or c.u.get("sale_eval_job") or c.u.get("last_job")
        return sale_bot.hand_reset(c, jid) if v.startswith("reset") else sale_bot.hand_ask(c, jid)
    if k == "eval" and sale_bot.on(c):
        store.update_user(c.uid, {"sale_edit": "eval", "sale_eval_job": v.partition("doc=")[2] or c.u.get("last_job")})
        return c.send(texts.build("sale_eval_ask"))
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
    if k == "req":  # ご要望・不具合のご連絡（youbou.py）
        import youbou
        return youbou.on_postback(c, v)
    if k == "cancel_reason":
        return c.send(growth.on_cancel_reason(c.uid, v) or line.text("ありがとうございます。"))
    if k == "choice":
        line.loading(c.line_uid, 20)
        return choice_press(c, v)
    if k == "recvfax" and v.split("&")[0] in ("send", "skip"):
        return recvfax_press(c, v)
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
        store.update_user(c.uid, {"pending": None, "req_wait": None, **({"sale_edit": None, "sale_eval_job": None} if c.u.get("sale_edit") else {}),
                                  **({"sale_tq": None} if (c.u.get("sale_tq") or {}).get("redo") else {})})
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


def plan_kw():
    """料金の文面の差し込み（売買版の件数・回数）。賃貸版では空。"""
    return texts._defaults()


def plan_show(c):
    """料金プランの案内。新人さん応援の期間中は、初めての方に案内を先に付ける（同じ応答で1通分）。"""
    msgs = [texts.build("plan_show", urls=plan_urls(c), **plan_kw())]
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
    import youbou
    if youbou.on_text(c, t):  # ご要望・不具合の受付中
        return
    if campaign.on_text(c, t):
        return
    import referral
    if referral.on_text(c, t):
        return
    step = current_step(c.u)
    if step:
        return reg_text(c, step, t)
    if sale_bot.on(c) and (c.u.get("sale_lq") or {}).get("step") == "amount" and sale_bot.lq_text(c, t):
        return
    if sale_bot.on(c) and (c.u.get("sale_tq") or {}).get("step") == "amount" and sale_bot.tq_text(c, t):
        return
    if sale_bot.on(c) and (c.u.get("sale_eq") or {}).get("step") == "wait" and sale_bot.eq_text(c, t):
        return
    if sale_bot.on(c) and c.u.get("sale_edit") and sale_bot.edit_text(c, t):
        return
    if norm(t) in naiken_bot.RESERVED_WORDS and c.u.get("registered"):
        return naiken_bot.show_reserved(c)
    if naiken_bot.active(c.u) or norm(t).startswith("内見"):
        if not c.u.get("registered"):
            return c.send(texts.build("reg_first"))
        if norm(t).startswith("内見") or not naiken_bot.active(c.u):
            store.update_user(c.uid, {"nk": {}, "nk_step": "free", "nk_fid": None})
            c.refresh()
        return naiken_bot.on_text(c, t)
    if c.u.get("name_wait") and not FIX_WORDS.search(t) and norm(t) not in MENU_WORDS \
            and norm(t).upper() not in PROMO and norm(t) != monitor.CODE and "@" not in t and "http" not in t:
        return on_name(c, t)
    # リッチメニューが使えないときも、文字で登録内容の変更を開けるように
    if c.u.get("registered") and norm(t) in ("変更", "登録内容の変更", "登録変更", "設定", "メニュー"):
        return on_postback(c, "reg=edit")
    if c.u.get("registered") and norm(t) in ("担当者", "担当者名", "担当者名の変更", "名前の変更", "携帯の変更"):
        return on_postback(c, "edit=staff")
    if c.u.get("registered") and FIX_WORDS.search(t) and c.u.get("last_job"):
        return fix_last(c, t)
    if monitor.ENABLED and norm(t) == monitor.CODE:
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
        if q["remaining"] is not None:  # 売買版のMAX（月の上限あり）：のこり10件以下でお知らせ
            return texts.note("remain_max", n=q["remaining"], 更新日=store.renew_date(u)) if q["remaining"] <= 10 else ""
        if q["month_used"] == store.UNLIMITED_SOFT_CAP + 1:
            return texts.note("unlimited_over")
        return ""
    n = q["remaining"]
    if q["plan"] == "light":
        mid = "remain_light_low" if n <= 5 else "remain_light"
        return texts.note(mid, n=n, 更新日=store.renew_date(u))
    exp = trial_until(u)  # 売買版のお試しの期限（賃貸版は空。文面にも差し込み先がない）
    if n == 0:
        return texts.note("remain_free_zero")
    if n == 1:
        return texts.note("remain_free_last", 期限=exp)
    return texts.note("remain_free", n=n, 期限=exp)


def trial_until(u):
    """お試しの期限の表示（例「11月30日（月）」）。期限がなければ空。"""
    e = (u or {}).get("free_expires")
    if not e:
        return ""
    d = e.astimezone(store.JST)
    return f"{d.month}月{d.day}日（{'月火水木金土日'[d.weekday()]}）"


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


def purge_pending():
    """定期実行から。預かって24時間を過ぎた募集図面の画像を消す（10/28本人決定）。消した件数を返す。"""
    n = 0
    for uid, u in store.db().before("users", "pending.at", store.now() - timedelta(hours=24)):
        if not u.get("pending"):
            continue
        store.db().delete_file(f"users/{uid}/pending.bin")
        store.update_user(uid, {"pending": None})
        n += 1
    return n


def process(c, data, held=None):
    """資料を作って届ける。held は預かっていた読み取り結果（事業用の確認後など）。"""
    q = store.quota(c.u)
    if q["remaining"] is not None and q["remaining"] <= 0:
        hold(c, data, "quota")
        mid = {"light": "plan_light_empty", "unlimited": "plan_max_empty"}.get(q["plan"], "plan_needed")  # MAX（売買版）
        return c.send(texts.build(mid, urls=plan_urls(c), 更新日=store.renew_date(c.u), **plan_kw()))
    # 費用の上限（AI利用料が月5万円）と、1人1日60件まで（10/29本人承認）
    if usage.ai_stopped():
        return c.send(usage.busy_message())
    if usage.over_daily(c.u):
        return c.send(usage.daily_limit_message())

    jid, job = store.new_job(c.uid, "line")
    store.update_job(jid, {"status": "processing"})
    if not held and asks_name(c.u):
        return analyze_and_ask(c, jid, data)
    done = threading.Event()

    def slow():
        if not done.wait(SLOW_SECONDS):
            c.send(texts.build("deliver_slow"))
    threading.Thread(target=slow, daemon=True).start()

    try:
        if held:
            img, _ = core.load_image(data)
            a = {"img": img, "pages": held.get("pages", 0), "result": Result.model_validate(held["result"]), "log": {}}
        else:
            if sale_bot.SALE_ONLY:  # 売買版のLINE：はじめから売買版で読む（AIは1回）。賃貸の図面なら賃貸版へご案内
                try:  # 時間がかかったときのお知らせ（deliver_slow）はそのまま生かす
                    return sale_bot.handle_sale(c, data, jid)
                finally:
                    done.set()
            a = core.analyze(data)
            v = core.verdict(a["result"])
            # 事業用の賃貸も、そのまま作る（お届けのときに一言添える）
            if v == "sale" and sale_bot.ENABLED:  # 売買版：売買の図面として読み直して届ける
                done.set()
                return sale_bot.handle_sale(c, data, jid, a)
            if v not in ("ok", "commercial"):
                done.set()
                store.update_job(jid, {"status": "failed", "error": v, "log": a["log"]})
                return c.send(texts.build({"blurry": "err_blurry", "not_maisoku": "err_not_maisoku",
                                           "sale": "err_sale"}[v]))
        done.set()
        finish(c, jid, a)
    except core.InputError as e:
        done.set()
        store.update_job(jid, {"status": "failed", "error": str(e)})
        c.send(line.text(str(e) + "\n\n※今回の分は、件数に数えていません。"))
    except Exception as e:
        done.set()
        log.exception("資料づくりで例外")
        store.update_job(jid, {"status": "failed", "error": repr(e)[:500]})
        c.send(texts.build("err_system"))


def finish(c, jid, a, atena="", after=()):
    """読み取り結果から資料を作り、件数を1つ使って届ける。atena は初期費用概算書の宛名（空なら空欄）。"""
    consumed = False
    try:
        if not store.try_consume(c.uid):
            store.update_job(jid, {"status": "failed", "error": "quota"})
            buf = io.BytesIO()
            a["img"].save(buf, "PNG")
            hold(c, buf.getvalue(), "quota", schema.for_store(a["result"]), a["pages"])
            return c.send(texts.build("plan_needed", urls=plan_urls(c)), *after)
        consumed = True
        if (c.u.get("plan") or "free") == "free" and store.quota(store.get_user(c.uid))["remaining"] == 0:
            growth.on_free_used_up(c.uid, c.u)  # 3日後にライトの案内（1回だけ）
        company = {**company_for_render(c.uid, c.u), "宛名": atena}
        out = core.render_documents(company, a["img"], a["result"], a["pages"], a["log"])
        store.save_files(jid, out)
        store.update_job(jid, {"status": "done", "title": out["title"], "texts": out["texts"], "log": out["log"],
                               "pages": out["pages"], "result": out["result"], "宛名": atena,
                               "verdict": core.verdict(a["result"])})
        recent = [{"id": jid, "title": out["title"], "at": store.now()}] + [r for r in (c.u.get("recent_jobs") or []) if r["id"] != jid]
        store.update_user(c.uid, {"last_job": jid, "pending": None, "recent_jobs": recent[:12]})
        c.refresh()
        deliver(c, jid, after=after)
        log.info("作成 %s %s", jid, json.dumps(out["log"], ensure_ascii=False))
    except Exception as e:
        log.exception("資料づくりで例外")
        if consumed:
            store.refund(c.uid)
        store.update_job(jid, {"status": "failed", "error": repr(e)[:500]})
        c.send(texts.build("err_system"), *after)


# ---------- 初期費用概算書の宛名（10/28本人決定） ----------
# 募集図面が届いたらすぐ宛名を聞き、その間に読み取りを進める。返事の名前を概算書の「＿＿様」に入れる。
# 「不要」なら空欄。「変更」から聞かない設定にできる（company.宛名を聞く）。

NO_NAME = {"不要", "ふよう", "なし", "無し", "空欄", "いらない", "要らない", "結構です", "けっこうです", "スキップ"}
NAME_WAIT_SECONDS = 90


def asks_name(u):
    return (u.get("company") or {}).get("宛名を聞く", True)


def _job_label(jid):
    """宛名をお聞きする物件の呼び方。読み取りが済んでいれば物件名。"""
    f = ((store.get_job(jid) or {}).get("result") or {}).get("事実") or {}
    name = " ".join(x for x in (f.get("物件名"), f.get("号室") and f"{f['号室']}号室") if x)
    return name or "先に送っていただいた募集図面"


def name_question(queue=(), kind="first"):
    """宛名の聞き方。kind: first（最初）・more（ほかに待ちがあるときに届いた）・next（1件届けたあと続けて）。"""
    if kind == "first" or not queue:
        return texts.build("atena_ask")
    if kind == "more":
        return texts.build("atena_more", 件数=len(queue), 物件=_job_label(queue[0]))
    return texts.build("atena_next", 件数=len(queue), 物件=_job_label(queue[0]))


def analyze_and_ask(c, jid, data):
    """宛名を聞いてから、読み取りを先に進めておく。結果は job に保存し、宛名が届いたら finish する。"""
    waiting = [j for j in (c.u.get("name_wait") or []) if j != jid]
    store.update_user(c.uid, {"name_wait": waiting + [jid]})
    c.send(name_question(waiting + [jid], "more") if waiting else name_question())
    try:
        if sale_bot.SALE_ONLY:
            return sale_bot.analyze_for_atena(c, jid, data, None)
        a = core.analyze(data)
        v = core.verdict(a["result"])
        if v == "sale" and sale_bot.on(c):  # 売買の図面：売買版で読み直して預かる（宛名が届いたら作る）
            return sale_bot.analyze_for_atena(c, jid, data, a)
        if v not in ("ok", "commercial"):
            store.update_job(jid, {"status": "failed", "error": v, "log": a["log"]})
            return
        buf = io.BytesIO()
        a["img"].save(buf, "PNG")
        store.db().put_file(f"jobs/{jid}/analyzed.png", buf.getvalue(), "image/png")
        store.update_job(jid, {"status": "analyzed", "result": schema.for_store(a["result"]),
                               "pages": a["pages"], "log": a["log"]})
    except core.InputError as e:
        store.update_job(jid, {"status": "failed", "error": "input:" + str(e)})
    except Exception as e:
        log.exception("読み取りで例外")
        store.update_job(jid, {"status": "failed", "error": repr(e)[:500]})


def parse_atena(t):
    s = unicodedata.normalize("NFKC", t or "").strip().strip("「」『』\"'")
    if s in NO_NAME or norm(s) in NO_NAME:
        return ""
    s = re.sub(r"\s*(様|さま|サマ|殿)$", "", s)
    return s[:30]


def on_name(c, t):
    """宛名の返事。いちばん古い待ちの資料に入れて作る。"""
    queue = list(c.u.get("name_wait") or [])
    jid = queue.pop(0)
    store.update_user(c.uid, {"name_wait": queue})
    c.refresh()
    atena = parse_atena(t)
    line.loading(c.line_uid, 60)
    after = [name_question(queue, "next")] if queue else []
    job = store.get_job(jid)
    t0 = time.monotonic()
    while job and job.get("status") == "processing" and time.monotonic() - t0 < NAME_WAIT_SECONDS:
        time.sleep(1.5)
        job = store.get_job(jid)
    if not job or job.get("status") == "processing":
        return c.send(texts.build("err_system"), *after)
    if job["status"] == "failed":
        err = job.get("error") or ""
        if err.startswith("input:"):
            return c.send(line.text(err[6:] + "\n\n※今回の分は、件数に数えていません。"), *after)
        if err.startswith("sale_"):
            return c.send(sale_bot.error_message(err), *after)
        mid = {"blurry": "err_blurry", "not_maisoku": "err_not_maisoku", "sale": "err_sale"}.get(err, "err_system")
        return c.send(texts.build(mid), *after)
    if job.get("kind") == "sale":
        return sale_bot.after_atena(c, jid, job, atena, after)
    png = store.db().get_file(f"jobs/{jid}/analyzed.png")
    a = {"img": Image.open(io.BytesIO(png)).convert("RGB"), "pages": job.get("pages") or 0,
         "result": Result.model_validate(job["result"]), "log": job.get("log") or {}}
    finish(c, jid, a, atena, after)


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

def deliver(c, jid, after=()):
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
    url_okyaku, url_shanai = store.pdf_urls(jid, job)
    img = line.image(store.file_url(jid, job, "obikae.jpg"), store.file_url(jid, job, "preview.jpg"))
    if job.get("verdict") == "commercial":
        # 事業用のときは、資料の前に一言（同じ応答なので通数は増えない）
        img = [texts.build("err_commercial"), img]
    if nofax(job):
        extra.append(NOFAX_NOTE)
    ask = choice_message(jid, job)
    if ask:
        after = (ask, *after)
    remain = "\n\n".join(x for x in extra + [remain] if x)
    # 何枚か続けて送られても、各資料の「この物件で内見FAX」はその物件で始まるよう資料IDを入れる
    common = dict(物件名=job["title"], のこり枚数の案内=remain, extra_buttons=remain_buttons(u), 資料ID=jid,
                  **nofax_drop(job))
    pre = []
    if method == "email" and d.get("email"):
        if delivery.send_mail(d["email"], job["title"], store.pdf_files(jid, job, job["title"]),
                              store.file_url(jid, job, "shiryou.pdf")):
            return c.send(*_as_list(img), texts.build("deliver_mail", メールアドレス=d["email"], **common), *after)
        if delivery.mail_ready():
            # 上限超えなどで送れなかったときは、LINEで届けて一言添える（運営にも知らせる）
            usage.count("mail_fallback")
            usage.notify_admin("メールが送れず、資料をLINEで届けました",
                               f"お客様ID：{c.uid}\n送り先：{d['email']}\n物件：{job['title']}\n"
                               f"Resendの上限（1日100通・月3,000通）に達したか、送り先の誤りの可能性があります。")
            pre = [texts.build("deliver_mail_failed", メールアドレス=d["email"])]
    if method == "fax" and d.get("fax"):
        # FAXの前に帯替え図面を見てもらい、「このままFAXする」で送る（10/28本人決定）
        t = store.now()
        store.update_job(jid, {"recvfax": "confirm", "recvfax_at": t})
        store.update_user(c.uid, {"recvfax_confirming": jid, "recvfax_remind_at": t + timedelta(minutes=REMIND_MINUTES)})
        return c.send(*_as_list(img), recvfax_confirm(jid, job), *after)
    c.send(*pre, *_as_list(img), texts.build("deliver_line", お客様向けリンク=url_okyaku, 社内向けリンク=url_shanai,
                                       有効期限=f"{exp.month}月{exp.day}日", **common), *after)


REMIND_MINUTES = 10
CONFIRM_HOURS = 24


def _links(jid, job):
    exp = job["expires_at"].astimezone(store.JST)
    url_okyaku, url_shanai = store.pdf_urls(jid, job)
    return (f"▼お客様向け（帯替え図面・初期費用の概算書）\n{url_okyaku}\n\n"
            f"▼社内向け（おすすめポイント・想定Q&A・内見申込書）\n{url_shanai}\n\n"
            f"※リンクは{exp.month}月{exp.day}日まで開けます。")


def recvfax_confirm(jid, job, links=True):
    return texts.build("recvfax_confirm", 物件名=job["title"], 資料ID=jid, append=_links(jid, job) if links else "",
                       **nofax_drop(job))


# ---------- 元付業者のFAX番号がない図面（精度検証B、10/30） ----------

NOFAX_NOTE = "この募集図面には、元付業者のFAX番号の記載がありません。内見のお申し込みは、元付業者に直接ご確認ください。"


def nofax(job):
    """募集図面に元付業者のFAX番号が書いていない（読み取りで空欄）。"""
    return not ((job.get("texts") or {}).get("motozuke") or {}).get("fax")


def nofax_drop(job):
    """FAX番号がない図面では「この物件で内見FAX」のボタンを出さない。"""
    return {"drop": ("postback naiken=from_last&doc={資料ID}",)} if nofax(job) else {}


# ---------- 条件が2通り以上ある図面（精度検証A、10/30） ----------

def choice_message(jid, job):
    """賃料などが区画・使い方で2通り以上書いてあり、まだ選ばれていなければ、どれで概算書を作るか聞く。"""
    f = (job.get("result") or {}).get("事実") or {}
    choices = f.get("条件が複数") or []
    if len(choices) < 2 or f.get("選んだ条件") is not None:
        return None
    names = "・".join(c["名前"] for c in choices)
    msg = line.text(f"この募集図面には、条件が{len(choices)}通り書いてあります（{names}）。\n"
                    "初期費用の概算書は、どれで作りますか？\n"
                    "お選びいただくまでは、条件によって違う金額を「要確認」にしています。")
    msg["quickReply"] = {"items": [line.qr_postback(c["名前"], f"choice={i}&doc={jid}")
                                   for i, c in enumerate(choices[:4])]}
    return msg


def apply_choice(uid, u, jid, i):
    """選んだ条件で概算書を作り直す（AIは使わない・件数に数えない）。戻り値は条件の名前（できなければ None）。"""
    job = store.get_job(jid)
    page = store.get_page(jid)
    if not job or job.get("status") != "done" or not page or job.get("user_id") != uid:
        return None
    r = Result.model_validate(job["result"])
    if not (0 <= i < len(r.事実.条件が複数)):
        return None
    r.事実 = costs.apply_choice(r.事実, i)
    company = {**company_for_render(uid, u), "宛名": job.get("宛名") or ""}
    out = core.render_documents(company, Image.open(io.BytesIO(page)).convert("RGB"), r,
                                job.get("pages") or 0, job.get("log") or {})
    store.save_files(jid, out)
    store.update_job(jid, {"result": out["result"], "log": out["log"], "choice": i})
    return r.事実.条件が複数[i].名前


def choice_press(c, v):
    i, _, rest = v.partition("&")
    jid = rest.partition("doc=")[2] or c.u.get("last_job")
    try:
        name = apply_choice(c.uid, c.u, jid, int(i))
    except ValueError:
        name = None
    if not name:
        return c.send(line.text("作り直す資料が見つかりませんでした（資料の保存期間が過ぎた可能性があります）。"))
    job = store.get_job(jid)
    return c.send(line.text(f"「{name}」の条件で、初期費用の概算書を作り直しました。\n\n" + _links(jid, job)))


def recvfax_press(c, v):
    """「このままFAXする」「LINEだけで受け取る」が押されたとき。v は「send&doc=資料ID」の形。"""
    how, _, rest = v.partition("&")
    jid = rest.partition("doc=")[2] or c.u.get("recvfax_confirming") or c.u.get("last_job")
    job = store.get_job(jid) if jid else None
    if not job or job.get("user_id") != c.uid:
        return c.send(line.text("お送りする資料が見つかりませんでした。お手数ですが、募集図面をもう一度送ってください。"))
    state = job.get("recvfax")
    if state == "sent":
        return c.send(line.text("この資料は、もうFAXでお送りしています。"))
    if state == "skip" and how == "skip":
        return c.send(line.text("この資料はFAXでお送りせず、LINEでのお届けのみにしています。"))
    if how == "skip":
        store.update_job(jid, {"recvfax": "skip"})
        return c.send(line.text("かしこまりました。この資料はFAXでお送りせず、LINEでのお届けのみにしました。\n"
                                "上のリンクから、いつでもご覧いただけます。"))
    if (job.get("recvfax_at") or store.now()) < store.now() - timedelta(hours=CONFIRM_HOURS):
        store.update_job(jid, {"recvfax": "expired"})
        return c.send(line.text("お届けから24時間が過ぎたため、この資料のFAXは終了しました。\n"
                                "FAXが必要な場合は、資料の「FAXにも送る」を押してください。"))
    d = c.u.get("delivery") or {}
    res, n = recvfax_send(c.uid, c.u, jid, job)
    if res == "sent":
        store.update_job(jid, {"recvfax": "sent"})
        c.refresh()
        add, btns = recvfax_low(c.uid, n)
        remain = "\n\n".join(x for x in (remain_note(c.u), add) if x)
        return c.send(texts.build("deliver_fax", FAX番号=d.get("fax", ""), 物件名=job["title"], 資料ID=jid,
                                  のこり枚数の案内=remain, extra_buttons=btns, **nofax_drop(job)))
    if res in ("empty", "trial_over"):
        # 回数券を買えば、この資料もFAXで送る
        store.update_user(c.uid, {"recvfax_wait": jid})
        store.update_job(jid, {"recvfax": "waiting"})
        mid = "recvfax_trial_over" if res == "trial_over" else "recvfax_empty"
        return c.send(texts.build(mid, urls=plan_urls(c)))
    return c.send(line.text("ただいまFAXでのお届けを準備中です。資料は上のリンクからご覧ください。"))


def remind_recvfax():
    """定期実行から。「このままFAXする」が10分押されていない方に、1回だけお知らせする。送った件数を返す。"""
    n = 0
    for uid, u in store.db().before("users", "recvfax_remind_at", store.now()):
        jid = u.get("recvfax_confirming")
        if not u.get("recvfax_remind_at"):
            continue
        store.update_user(uid, {"recvfax_confirming": None, "recvfax_remind_at": None})
        job = store.get_job(jid) if jid else None
        if not job or job.get("recvfax") != "confirm" or not u.get("line_uid") or u.get("blocked"):
            continue
        if job.get("recvfax_at") and job["recvfax_at"] < store.now() - timedelta(hours=CONFIRM_HOURS):
            continue
        line.push(u["line_uid"], [texts.build("recvfax_remind", 物件名=job["title"], 資料ID=jid, **nofax_drop(job))])
        n += 1
    return n


def _as_list(x):
    return x if isinstance(x, list) else [x]


TRIAL_FAX = 3  # お試し中の受取りFAXは最初の3件まで無料（10/28本人決定）


def recvfax_send(uid, u, jid, job):
    """受け取り用FAXで、資料の2枚（帯替え図面＋初期費用概算書）を御社のFAXへ送る。
    お試し中は最初の3件まで回数券なしで送る。戻り値は (結果, のこり回数)。
    結果は "sent"・"empty"（回数券なし）・"trial_over"（お試しの3件を使い切り、回数券もない）・None（送れない）。"""
    number = (u.get("delivery") or {}).get("fax")
    if not (number and (delivery.fax_ready() or u.get("review"))):
        return None, None
    company = company_for_render(uid, u)
    if (u.get("plan") or "free") == "free":
        if (u.get("trial_fax_used") or 0) < TRIAL_FAX:
            ok = delivery.send_fax(number, faxopt.build_pdf(company, {**job, "_id": jid}),
                                    kind="recv", ref=jid, uid=uid, meta={"trial": True})
            if ok:
                store.update_user(uid, {"trial_fax_used": (u.get("trial_fax_used") or 0) + 1})
            return ("sent", None) if ok else (None, None)
        if not faxopt.can_send(u):
            return "trial_over", None
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


MENU_WORDS = {"変更", "登録内容の変更", "登録変更", "設定", "メニュー", "担当者", "担当者名", "担当者名の変更",
              "名前の変更", "携帯の変更"}
FIX_WORDS = re.compile(r"消して|消す|消去|削除|隠して|見えないように|外して|取って|塗りつぶ")


def do_fix(uid, u, jid, t):
    """「ここを消して」の直し（LINE・アプリ共通）。頼まれた文字を白で消して資料を作り直す。
    件数には数えない（10/28本人決定：何度でも無料）。
    戻り値: {"status": "done" | "none" | "missing" | "expired", "texts": [...], "note": できないことの説明}"""
    job = store.get_job(jid) if jid else None
    usage.count("fix")
    if not job or job.get("status") != "done":
        return {"status": "missing", "texts": [], "note": None}
    if job["expires_at"] < store.now():
        return {"status": "expired", "texts": [], "note": None}
    page = store.get_page(jid)
    ob = store.db().get_file(f"jobs/{jid}/obikae.png")
    if not page or not ob:
        return {"status": "expired", "texts": [], "note": None}
    r = core.fix(company_for_render(uid, u), Image.open(io.BytesIO(page)).convert("RGB"),
                 Result.model_validate(job["result"]), ob, t, job.get("pages") or 0)
    fixes = (job.get("fixes") or []) + [{"at": store.now(), **{k: v for k, v in r["log"].items()
                                                              if k in ("直しの依頼", "消す文字", "直せなかったこと", "原価_円")}}]
    if not r["out"]:
        store.update_job(jid, {"fixes": fixes})
        return {"status": "none", "texts": [], "note": r["note"]}
    out = r["out"]
    store.save_files(jid, out)
    store.update_job(jid, {"result": out["result"], "log": out["log"], "fixes": fixes})
    log.info("直し %s %s", jid, json.dumps(r["log"], ensure_ascii=False, default=str))
    return {"status": "done", "texts": r["texts"], "note": r["note"]}


def rerender_atena(uid, u, jid, atena):
    """できあがった資料の宛名だけを差し替えて作り直す（AIは使わない・件数に数えない）。アプリの宛名入力用。"""
    job = store.get_job(jid)
    page = store.get_page(jid)
    if not job or job.get("status") != "done" or not page:
        return False
    company = {**company_for_render(uid, u), "宛名": atena}
    out = core.render_documents(company, Image.open(io.BytesIO(page)).convert("RGB"),
                                Result.model_validate(job["result"]), job.get("pages") or 0, job.get("log") or {})
    store.save_files(jid, out)
    store.update_job(jid, {"宛名": atena, "log": out["log"]})
    return True


def fix_last(c, t):
    """LINEで「〇〇を消して」と送られたとき。直近の資料を直して、帯替え図面とPDFを送り直す。"""
    jid = c.u.get("last_job")
    line.loading(c.line_uid, 60)
    try:
        r = do_fix(c.uid, c.u, jid, t)
    except Exception:
        log.exception("直しで例外")
        return c.send(texts.build("err_system"))
    if r["status"] in ("missing", "expired"):
        return c.send(line.text("直せる資料が見つかりませんでした（資料の保存期間が過ぎた可能性があります）。お手数ですが、募集図面をもう一度送ってください。"))
    if r["status"] == "none":
        return c.send(line.text(r["note"] or "ご依頼の文字が図面の中に見つかりませんでした。消したいところを、図面に書いてある言葉でもう一度送ってください。"))
    job = store.get_job(jid)
    exp = job["expires_at"].astimezone(store.JST)
    url_okyaku, url_shanai = store.pdf_urls(jid, job)
    append = (f"PDFも直しました（{exp.month}月{exp.day}日まで開けます）。\n▼お客様向け\n{url_okyaku}\n▼社内向け\n{url_shanai}"
              if url_okyaku != url_shanai else
              f"資料一式（PDF）も直しました：\n{url_okyaku}\n（{exp.month}月{exp.day}日まで）")
    if r["note"]:
        append = r["note"] + "\n\n" + append
    v = f"?v={len(job.get('fixes') or [])}"  # LINEに前の画像を出させないため
    img = line.image(store.file_url(jid, job, "obikae.jpg") + v, store.file_url(jid, job, "preview.jpg") + v)
    done = texts.build("fix_done", 物件名=job.get("title") or "", 直した内容="、".join(r["texts"]), append=append)
    if job.get("recvfax") == "confirm":
        return c.send(img, done, recvfax_confirm(jid, job, links=False))
    c.send(img, done)


def send_last(c, how):
    """届いた資料を、別の送り先にも送る（「メールにも送る」など）。"""
    jid = c.u.get("last_job")
    job = store.get_job(jid) if jid else None
    if not job or job.get("status") != "done":
        return c.send(line.text("お送りできる資料が見つかりませんでした。募集図面を送ってください。"))
    d = c.u.get("delivery") or {}
    if how == "line":
        exp = job["expires_at"].astimezone(store.JST)
        url_okyaku, url_shanai = store.pdf_urls(jid, job)
        return c.send(texts.build("deliver_line", 物件名=job["title"], お客様向けリンク=url_okyaku, 社内向けリンク=url_shanai,
                                  有効期限=f"{exp.month}月{exp.day}日", のこり枚数の案内="", 資料ID=jid,
                                  **nofax_drop(job)))
    if how == "mail":
        if not d.get("email"):
            store.update_user(c.uid, {"reg_queue": ["send_mail"], "reg_mode": "send_mail"})
            return c.send(texts.build("send_mail_ask"))
        if delivery.send_mail(d["email"], job["title"], store.pdf_files(jid, job, job["title"]),
                              store.file_url(jid, job, "shiryou.pdf")):
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
    if kw and kw.get("_sale"):  # 売買の物件確認・内見依頼書FAXのお知らせ：言い方だけ売買用に
        import naiken
        kw = {k: v for k, v in kw.items() if k != "_sale"}
        return _notify(uid, mid, kw, wrap=naiken.sale_words)
    return _notify(uid, mid, kw)


def _notify(uid, mid, kw, wrap=None):
    u = store.get_user(uid)
    if u and mid == "plan_canceled":
        store.update_user(uid, {"ask_cancel_reason": True})  # アプリは /v1/me でこれを見て1回聞く
    if not u or not u.get("line_uid"):
        return
    if mid == "plan_started":
        q = store.quota(u)
        import naiken
        body = {"light": f"毎月、募集図面{config.LIGHT_SHEETS}件分の資料と内見申込FAX{naiken.LIMITS['light'][0]}回をお使いいただけます。",
                "unlimited": (f"毎月、販売図面{config.MAX_SHEETS}件分の資料と内見依頼FAX{naiken.LIMITS['unlimited'][0]}回をお使いいただけます。"
                              if config.MAX_SHEETS else
                              f"資料は件数を気にせず、内見申込FAXは月{naiken.LIMITS['unlimited'][0]}回までお使いいただけます。")}.get(q["plan"], "")
        kw = {"プラン名": billing.plan_name(u), "プランの内容": body, "更新日": store.billing_renew_date(u)}
    elif mid == "plan_canceled":
        kw = {"期限": store.renew_date(u)}
    elif mid == "plan_payment_failed":
        kw = {"猶予日": kw.get("猶予日") or "1週間後", "urls": {"お支払い情報を確認": billing.portal_url(uid)}}
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
    msgs = [texts.build(mid, urls=urls, **kw)]
    if wrap:
        msgs = [wrap(m) for m in msgs]
    if mid == "plan_canceled":
        msgs.append(growth.cancel_question())  # 同じ push なので通数は増えない
    if not line.push(u["line_uid"], msgs):
        push_failed(uid, u, mid, kw, urls)
    if mid == "recvfax_done" and u.get("recvfax_wait"):
        pool.submit(_send_waiting_fax, uid)
    if mid == "plan_started" and u.get("pending"):
        c = Ctx({"source": {"userId": u["line_uid"]}})
        pool.submit(_safe_pending, c, None)


# お金にかかわるお知らせは、LINEで送れなかったときメールでも送る（10/28本人決定）
MONEY_NOTICES = {"plan_payment_failed", "plan_started", "plan_upgraded", "plan_downgrade_scheduled", "plan_canceled",
                 "plan_annual_renew", "recvfax_done", "addfax_done", "shinjin_started"}
IMPORTANT_NOTICES = {"naiken_sent", "naiken_failed"}


def push_failed(uid, u, mid, kw, urls=None):
    """push が送れなかったとき（上限など）。お知らせ自体は usage.hold でためてあり、次の応答で届く。"""
    if mid in MONEY_NOTICES:
        to = (u.get("delivery") or {}).get("email") or (u.get("company") or {}).get("担当者メール")
        if to:
            body = texts.raw(mid, **kw)
            if urls:
                body += "\n\n" + "\n".join(f"{k}：{v}" for k, v in urls.items())
            delivery.send_notice(to, "【1分資料】大切なお知らせ", body + "\n\n――――――――――\n1分資料（otpromo.com）\n"
                                 "LINEでお送りできなかったため、メールでお届けしました。")
    if mid in IMPORTANT_NOTICES:
        usage.notify_admin("内見申込FAXの結果をLINEで送れませんでした",
                           f"お客様ID：{uid}\n会社：{(u.get('company') or {}).get('会社名', '')}\n"
                           f"お知らせ：{mid}\n\n{texts.raw(mid, **kw)}")


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
