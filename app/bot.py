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
import config
import core
import delivery
import line
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
REG_STEPS = ["company", "shop", "license", "tel", "fax", "address", "fee", "color", "font", "obi", "receive"]
EDIT_STEPS = {"company": ["company", "shop"], "license": ["license"], "tel": ["tel", "fax"],
              "address": ["address"], "fee": ["fee"], "color": ["color"], "font": ["font"],
              "obi": ["obi"], "recv": ["receive"]}
EDIT_NAMES = {"company": "会社名・店舗名", "license": "免許番号", "tel": "電話・FAX", "address": "住所",
              "fee": "仲介手数料", "color": "テーマ色", "font": "書体", "obi": "帯の画像", "recv": "受け取り方"}
ASK = {"company": "reg_company", "shop": "reg_shop", "license": "reg_license", "tel": "reg_tel",
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
        return store.update_user(c.uid, {"blocked": True})
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


# ---------- 友だち追加 ----------

def on_follow(c):
    store.update_user(c.uid, {"blocked": False})
    if c.u.get("registered"):
        link_menu(c.line_uid, True)
        return c.send(texts.build("refollow", 会社名=c.u["company"].get("会社名", ""), のこり枚数の案内=remain_note(c.u)))
    link_menu(c.line_uid, False)
    name = line.profile(c.line_uid).get("displayName", "")
    c.send(texts.build("follow", 表示名=name))


def link_menu(line_uid, registered):
    rid = RICHMENU_REGISTERED if registered else RICHMENU_UNREGISTERED
    if rid:
        try:
            import requests
            requests.post(f"{line.API}/user/{line_uid}/richmenu/{rid}", headers=line._h(), timeout=10)
        except Exception:
            log.warning("リッチメニューの切り替えに失敗")


# ---------- 登録 ----------

def start_steps(c, steps, mode):
    store.update_user(c.uid, {"reg_queue": steps, "reg_mode": mode})
    c.refresh()
    ask(c, steps[0])


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
    if mode == "send_mail":
        store.update_user(c.uid, {"reg_mode": None})
        return send_last(c, "mail")
    return c.send(confirm_message(c.u))


def confirm_message(u, mid="reg_confirm"):
    co = u.get("company") or {}
    d = u.get("delivery") or {}
    recv = {"line": "LINE", "email": f"メール（{d.get('email', '')}）", "fax": f"FAX（{d.get('fax', '')}）"}
    return texts.build(mid, 会社名=co.get("会社名", ""), 店舗名=co.get("店舗名") or "なし",
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
        store.update_user(c.uid, {"agreed_at": store.now()})
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
    if k == "edit" and v in EDIT_STEPS:
        store.update_user(c.uid, {"reg_edit_name": EDIT_NAMES[v]})
        mode = "edit" if c.u.get("registered") else "new"
        return start_steps(c, list(EDIT_STEPS[v]), mode)
    if k in ("shop", "license", "fax", "fee", "color", "font", "obi", "recv"):
        return reg_postback(c, k, v)
    if k == "help":
        return c.send(texts.build({"usage": "help_usage", "contact": "help_contact", "share": "help_share",
                                   "menu": "help_menu"}.get(v, "help_usage")))
    if k == "plan" and v == "show":
        return c.send(plan_show(c))
    if k == "plan" and v == "status":
        return c.send(plan_status(c))
    if k == "send":
        return send_last(c, v)
    if k == "commercial" and v == "go":
        return pool.submit(_safe_pending, c, "commercial")
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
            "使い放題を申し込む": billing.pay_url(c.uid, "unlimited"),
            "使い放題に切り替える": billing.pay_url(c.uid, "unlimited"),
            "FAXオプションを申し込む": billing.pay_url(c.uid, "fax"),
            "変更・解約": billing.portal_url(c.uid),
            "お支払い情報を確認": billing.portal_url(c.uid)}


def plan_show(c):
    return texts.build("plan_show", urls=plan_urls(c))


def plan_status(c):
    q = store.quota(c.u)
    d = c.u.get("delivery") or {}
    return texts.build("plan_status", urls=plan_urls(c), プラン名=billing.PLAN_NAMES[q["plan"]],
                       n="上限なし" if q["remaining"] is None else q["remaining"],
                       更新日=store.renew_date(c.u),
                       受け取り方={"line": "LINE", "email": "メール", "fax": "FAX"}.get(d.get("method"), "LINE"))


# ---------- 文字 ----------

def on_text(c, t):
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
    code = norm(t).upper()
    if code in PROMO:
        if store.apply_promo(c.uid, code, int(PROMO[code])):
            n = config.FREE_SHEETS + int(PROMO[code])
            return c.send(line.text(f"合言葉を受け付けました！\n無料でお試しいただける枚数が{n}枚になりました。"))
        return c.send(line.text("合言葉は、おひとり様1回までのご利用です。"))
    c.send(texts.build("err_text"))


# ---------- のこり枚数の案内 ----------

def remain_note(u):
    q = store.quota(u)
    if q["plan"] == "unlimited":
        return ""
    n = q["remaining"]
    if q["plan"] == "light":
        mid = "remain_light_low" if n <= 3 else "remain_light"
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
            if v != "ok":
                done.set()
                store.update_job(jid, {"status": "failed", "error": v, "log": a["log"]})
                if v == "commercial":
                    hold(c, data, "commercial", json.loads(a["result"].model_dump_json()), a["pages"])
                return c.send(texts.build({"blurry": "err_blurry", "not_maisoku": "err_not_maisoku",
                                           "sale": "err_sale", "commercial": "err_commercial"}[v]))
        if not store.try_consume(c.uid):
            done.set()
            store.update_job(jid, {"status": "failed", "error": "quota"})
            return c.send(texts.build("plan_needed", urls=plan_urls(c)))
        consumed = True
        out = core.render_documents(company_for_render(c.uid, c.u), a["img"], a["result"], a["pages"], a["log"])
        store.save_files(jid, out)
        store.update_job(jid, {"status": "done", "title": out["title"], "texts": out["texts"], "log": out["log"],
                               "pages": out["pages"], "result": out["result"]})
        recent = [{"id": jid, "title": out["title"]}] + [r for r in (c.u.get("recent_jobs") or []) if r["id"] != jid]
        store.update_user(c.uid, {"last_job": jid, "pending": None, "recent_jobs": recent[:10]})
        c.refresh()
        done.set()
        deliver(c, jid)
        log.info("作成 %s %s", jid, json.dumps(out["log"], ensure_ascii=False))
    except core.InputError as e:
        done.set()
        if consumed:
            store.refund(c.uid)
        store.update_job(jid, {"status": "failed", "error": str(e)})
        c.send(line.text(str(e) + "\n\n※今回の分は、枚数に数えていません。"))
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
            c.send(line.text("お預かりしていたマイソクの期限が切れました。お手数ですが、もう一度送ってください。"))
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
    remain = "\n\n".join(x for x in extra + [remain] if x)
    common = dict(物件名=job["title"], のこり枚数の案内=remain, extra_buttons=remain_buttons(u))
    if method == "email" and d.get("email"):
        pdf = store.db().get_file(f"jobs/{jid}/shiryou.pdf")
        if delivery.send_mail(d["email"], job["title"], pdf, pdf_url):
            return c.send(img, texts.build("deliver_mail", メールアドレス=d["email"], **common))
    if method == "fax" and d.get("fax") and delivery.fax_ready():
        pdf = store.db().get_file(f"jobs/{jid}/shiryou.pdf")
        if delivery.send_fax(d["fax"], pdf):
            return c.send(img, texts.build("deliver_fax", FAX番号=d["fax"], **common))
    c.send(img, texts.build("deliver_line", リンク=pdf_url, 有効期限=f"{exp.month}月{exp.day}日", **common))


def send_last(c, how):
    """届いた資料を、別の送り先にも送る（「メールにも送る」など）。"""
    jid = c.u.get("last_job")
    job = store.get_job(jid) if jid else None
    if not job or job.get("status") != "done":
        return c.send(line.text("お送りできる資料が見つかりませんでした。マイソクを送ってください。"))
    d = c.u.get("delivery") or {}
    if how == "line":
        exp = job["expires_at"].astimezone(store.JST)
        return c.send(texts.build("deliver_line", 物件名=job["title"], リンク=store.file_url(jid, job, "shiryou.pdf"),
                                  有効期限=f"{exp.month}月{exp.day}日", のこり枚数の案内=""))
    if how == "mail":
        if not d.get("email"):
            store.update_user(c.uid, {"reg_queue": ["send_mail"], "reg_mode": "send_mail"})
            return c.send(texts.build("send_mail_ask"))
        pdf = store.db().get_file(f"jobs/{jid}/shiryou.pdf")
        if delivery.send_mail(d["email"], job["title"], pdf, store.file_url(jid, job, "shiryou.pdf")):
            return c.send(texts.build("send_mail_done", メールアドレス=d["email"]))
        return c.send(line.text("ただいまメールでのお届けを準備中です。もうしばらくお待ちください。"))
    if how == "fax":
        trial = (c.u.get("plan") or "free") == "free"
        if not (c.u.get("fax_option") or trial):
            return c.send(texts.build("send_fax_option", urls=plan_urls(c)))
        if not d.get("fax"):
            store.update_user(c.uid, {"reg_queue": ["faxto"], "reg_mode": "edit", "reg_edit_name": "FAXの送り先"})
            return c.send(texts.build("reg_faxto"))
        pdf = store.db().get_file(f"jobs/{jid}/shiryou.pdf")
        if delivery.fax_ready() and delivery.send_fax(d["fax"], pdf):
            return c.send(texts.build("send_fax_done", FAX番号=d["fax"]))
        return c.send(line.text("ただいまFAXでのお届けを準備中です。もうしばらくお待ちください。"))


# ---------- Stripe からの通知を LINE で知らせる ----------

def notify(uid, mid, kw):
    u = store.get_user(uid)
    if not u or not u.get("line_uid"):
        return
    if mid == "plan_started":
        q = store.quota(u)
        body = {"light": f"毎月{config.LIGHT_SHEETS}枚までお使いいただけます。",
                "unlimited": "枚数を気にせずお使いいただけます。"}.get(q["plan"], "")
        kw = {"プラン名": billing.PLAN_NAMES[q["plan"]], "プランの内容": body, "更新日": store.renew_date(u)}
    elif mid == "plan_canceled":
        kw = {"期限": store.renew_date(u)}
    elif mid == "plan_payment_failed":
        kw = {"猶予日": "1週間後", "urls": {"お支払い情報を確認": billing.portal_url(uid)}}
    urls = kw.pop("urls", None)
    line.push(u["line_uid"], [texts.build(mid, urls=urls, **kw)])
    if mid == "plan_started" and u.get("pending"):
        c = Ctx({"source": {"userId": u["line_uid"]}})
        pool.submit(_safe_pending, c, None)
