"""内見申込書FAXの、LINEでの会話。文面は messages.json の naiken_* を使う。

利用者の状態：nk（下書き）、nk_step（いま聞いている項目）、nk_fid（確認中の申込書）
"""
import logging
import re
import unicodedata
from datetime import timedelta

import line
import naiken
import store
import texts

log = logging.getLogger("naiken_bot")

ASK_ONLY = {"prop": "物件名・号室・所在地", "dates": "内見の希望日時", "to": "管理会社の名前とFAX番号",
            "fax_invalid": "管理会社の名前とFAX番号", "edit_to": "管理会社の名前とFAX番号",
            "edit_fax": "管理会社のFAX番号", "edit_prop": "物件名・号室・所在地", "edit_dates": "内見の希望日時"}


def active(u):
    return bool(u.get("nk_step"))


def _set(c, **kv):
    store.update_user(c.uid, kv)
    c.refresh()


def start(c):
    _set(c, nk={}, nk_step="free", nk_fid=None)
    c.send(texts.build("naiken_start"))


def start_from_job(c, jid):
    job = store.get_job(jid) if jid else None
    if not job or job.get("status") != "done" or job.get("user_id") != c.uid:
        return c.send(line.text("お作りした資料が見つかりませんでした。物件名と号室を教えてください。"),
                      texts.build("naiken_ask_property"))
    job["_id"] = jid
    d = naiken.from_job(job)
    to = d["to"]
    p = d.get("prop") or {}
    if not (to.get("company") and naiken.fax_ok(to.get("fax"))):
        _set(c, nk=d, nk_step="to")
        msg = texts.build("naiken_fax_unread", 物件名=room_text(d))
        if to.get("company"):
            # 管理会社名だけ読めたときは短く聞く
            msg = texts.build("naiken_fax_unread", text=f"{to_text(to)}のFAX番号を教えてください。")
        return c.send(msg)
    _set(c, nk=d, nk_step="dates")
    text = texts.raw("naiken_from_last", 物件名=p.get("name", ""), 号室=p.get("room") or "",
                     管理会社=to_text(to), FAX番号=naiken.fax_display(to["fax"]))
    if not p.get("room"):
        text = text.replace(" 号室", "")
    c.send(texts.build("naiken_from_last", text=text))


def short_label(title):
    """ボタンの20文字に収める。物件名を短くして、号室は残す。"""
    t = (title or "物件").strip()
    if len(t) <= 20:
        return t
    m = re.search(r"\s*\S*号室$", t)
    tail = m.group().strip() if m else ""
    return (t[:20 - len(tail) - 2] + "… " + tail) if tail else t[:19] + "…"


def room_text(d):
    p = d.get("prop") or {}
    return f"{p.get('name', '')} {p['room']}号室" if p.get("room") else p.get("name", "")


def to_text(to):
    return " ".join(x for x in (to.get("company"), to.get("branch")) if x)


def on_text(c, t):
    """内見FAXの流れの中で文字が届いたとき。"""
    step = c.u.get("nk_step") or "free"
    d = c.u.get("nk") or {}
    t = t.strip()
    co = dict(c.u.get("company") or {})
    if step.startswith("card_"):
        return card_text(c, step, t, co)
    if step == "edit_guests":
        n = re.search(r"\d+", unicodedata.normalize("NFKC", t))
        d["guests"] = int(n.group()) if n else None
    elif step == "edit_guide":
        d["guide"] = None if t in ("なし", "本人", "自分") else t[:20]
    else:
        line.loading(c.line_uid, 20)
        try:
            parsed = naiken.ai_parse(t)
        except Exception:
            log.exception("内見の文の読み取りに失敗")
            return c.send(texts.build("err_system"))
        if step in ("edit_dates", "dates") and parsed.get("wishes"):
            d["wishes"] = []
        d = naiken.merge(d, parsed)
        d["wishes"] = (d.get("wishes") or [])[:2]  # 内見の希望日時は第2希望まで
        if not parsed.get("to_fax"):
            # AIが番号を拾えなかったときは、電話番号の形をした最後のものをFAX番号とみなす
            nums = re.findall(r"\+?\d[\d\-‐－ー()（） ]{8,16}\d", unicodedata.normalize("NFKC", t))
            nums = [x for x in nums if 9 <= len(naiken.normalize_fax(x)) <= 12]
            if nums:
                d.setdefault("to", {})["fax"] = nums[-1].strip()
                d["to"]["from_maisoku"] = False
    _set(c, nk=d)
    next_step(c)


def next_step(c, prefix=None):
    d = c.u.get("nk") or {}
    m = naiken.missing(d)
    pre = [prefix] if prefix else []
    if m == "prop":
        _set(c, nk_step="prop")
        return c.send(*pre, texts.build("naiken_ask_property"))
    if m == "dates":
        _set(c, nk_step="dates")
        return c.send(*pre, texts.build("naiken_ask_dates"))
    if m == "to":
        _set(c, nk_step="to")
        return c.send(*pre, texts.build("naiken_ask_to"))
    if m == "fax_invalid":
        bad = d["to"].get("fax", "")
        d["to"]["fax"] = ""
        _set(c, nk=d, nk_step="fax_invalid")
        return c.send(*pre, texts.build("naiken_fax_invalid", 入力された番号=bad))
    cm = naiken.card_missing(c.u.get("company") or {})
    if cm:
        _set(c, nk_step=cm[0])
        return c.send(*pre, ask_card(cm[0], first=True))
    confirm(c, pre)


def confirm(c, pre=()):
    d = c.u.get("nk") or {}
    co = c.u.get("company") or {}
    fid, rec = naiken.save_preview(c.uid, d, co)
    _set(c, nk_step="confirm", nk_fid=fid)
    to = d["to"]
    wishes = [naiken.wish_label(w) for w in d.get("wishes", [])]
    kw = {"管理会社": to_text(to), "FAX番号": naiken.fax_display(to["fax"]),
          "読み取り元": "（募集図面から読み取りました）" if to.get("from_maisoku") else "",
          "物件名": (d.get("prop") or {}).get("name", ""), "号室": (d.get("prop") or {}).get("room") or "",
          "人数": d.get("guests") or "", "ご案内担当": d.get("guide") or co.get("担当者名") or ""}
    for i in range(3):
        kw[f"日時{i + 1}"] = wishes[i] if i < len(wishes) else ""
    text = texts.raw("naiken_confirm", **kw)
    # 入っていない項目の行は出さない
    drop = re.compile(r"^(第\d希望：|■お客様：名|■ご案内：)\s*$")
    text = "\n".join(ln for ln in text.split("\n") if not drop.match(ln.strip()))
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not kw["号室"]:
        text = text.replace(" 号室", "")
    img = line.image(naiken.file_url(fid, rec, "naiken.png"), naiken.file_url(fid, rec, "naiken.png"))
    msg = texts.build("naiken_confirm", text=text)
    c.send(*pre, img, msg)


# ---------- 名刺の情報 ----------

CARD_ASK = {
    "card_name": "naiken_card_intro", "card_roman": "naiken_card_roman", "card_role": "naiken_card_role",
    "card_contact": "naiken_card_contact", "card_fax": "naiken_card_fax",
}


def ask_card(step, first=False):
    if step == "card_name" and not first:
        return line.text("ご担当者様のお名前を教えてください。\n（例：山田 花子）")
    return texts.build(CARD_ASK[step])


def card_text(c, step, t, co):
    none = t in ("なし", "無し", "ない")
    t = unicodedata.normalize("NFKC", t).strip() if step != "card_name" else t.strip()
    if step == "card_name":
        co["担当者名"] = t[:20]
    elif step == "card_roman":
        co["ローマ字"] = "" if none else t[:40]
    elif step == "card_role":
        co["役職"] = "" if none else t[:40]
    elif step == "card_contact":
        if not none:
            m = re.search(r"0[789]0-?\d{4}-?\d{4}", t)
            e = re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", t)
            co["携帯"] = m.group() if m else ""
            co["メール"] = e.group() if e else ""
    elif step == "card_fax":
        if not naiken.fax_ok(t):
            return c.send(texts.build("naiken_card_fax"))
        co["FAX"] = naiken.fax_display(t)
    store.update_user(c.uid, {"company": co})
    c.refresh()
    card_next(c, step)


def card_postback(c, k):
    co = dict(c.u.get("company") or {})
    field = {"card_roman": "ローマ字", "card_role": "役職", "card_contact": None}[k]
    if field:
        co[field] = ""
    store.update_user(c.uid, {"company": co})
    c.refresh()
    card_next(c, k)


def card_next(c, step):
    order = naiken.CARD_STEPS
    co = dict(c.u.get("company") or {})
    nxt = None
    for s in order[order.index(step) + 1:]:
        if s == "card_fax" and co.get("FAX"):
            continue
        nxt = s
        break
    if nxt:
        _set(c, nk_step=nxt)
        return c.send(ask_card(nxt))
    co["名刺済み"] = True
    store.update_user(c.uid, {"company": co})
    c.refresh()
    done = texts.build("naiken_card_done")
    if (c.u.get("nk") or {}).get("card_only"):
        _set(c, nk_step=None, nk={})
        return c.send(done)
    next_step(c, prefix=done)


# ---------- ボタン ----------

EDIT_ASK = {"edit_to": "naiken_ask_to", "edit_fax": "naiken_ask_to", "edit_prop": "naiken_ask_property",
            "edit_dates": "naiken_ask_dates"}


def on_postback(c, v):
    if v == "start":
        return start(c)
    if v == "from_last":
        return start_from_job(c, c.u.get("last_job"))
    if v.startswith("from_last&doc="):
        return start_from_job(c, v.split("=", 1)[1])
    if v.startswith("job:"):
        return start_from_job(c, v[4:])
    if v == "recent":
        # 直近7日の資料を新しい順に最大12件
        since = store.now() - timedelta(days=7)
        rec = [r for r in (c.u.get("recent_jobs") or []) if not r.get("at") or r["at"] >= since][:12]
        if not rec:
            return c.send(line.text("最近お作りした物件がありません。募集図面を送ってください。",
                                    quick=[{"type": "action", "action": {"type": "camera", "label": "募集図面を送る"}}]))
        q = [line.qr_postback(short_label(r["title"]), f"naiken=from_last&doc={r['id']}") for r in rec]
        return c.send(texts.build("naiken_recent", text=texts.raw("naiken_recent")) | {"quickReply": {"items": q}})
    if v == "history":
        h = c.u.get("naiken_history") or []
        if not h:
            return c.send(line.text("まだ送った宛先がありません。管理会社の名前とFAX番号を教えてください。"))
        q = [line.qr_postback(x["company"][:20] or naiken.fax_display(x["fax"]), f"naiken=to:{i}") for i, x in enumerate(h[:10])]
        return c.send(line.text("宛先を選んでください。", quick=q))
    if v.startswith("to:"):
        h = c.u.get("naiken_history") or []
        i = int(v[3:])
        if i < len(h):
            d = c.u.get("nk") or {}
            d["to"] = {**h[i], "from_maisoku": False}
            _set(c, nk=d)
        return next_step(c)
    if v == "send":
        fid = c.u.get("nk_fid")
        if not fid:
            return c.send(line.text("送る内容が見つかりませんでした。もう一度はじめからお願いします。"))
        status = naiken.send(c.uid, fid)
        rec = store.db().get("faxes", fid)
        if status == "sending":
            _set(c, nk_step=None, last_naiken=fid)
            return c.send(texts.build("naiken_sending"))
        if status == "limit":
            import bot
            period, limit, nxt = naiken.limit_info(c.u)
            if period == "本日":
                return c.send(texts.build("naiken_limit_day", 上限=limit))
            return c.send(texts.build("naiken_limit", urls=bot.plan_urls(c), 上限=limit, 更新日=nxt))
        if status == "fax_invalid":
            return next_step(c)
        if status == "not_ready":
            _set(c, nk_step=None, last_naiken=fid)
            return c.send(line.text("ただいまFAX送信の準備中のため、まだお送りしていません（テスト版）。\n"
                                    f"作った申込書はこちらから見られます。\n{naiken.file_url(fid, rec, 'naiken.pdf')}"))
        return c.send(line.text("この申込書は、すでに送信の手続きが済んでいます。"))
    if v == "edit":
        return c.send(texts.build("naiken_edit"))
    if v in EDIT_ASK:
        _set(c, nk_step=v)
        return c.send(texts.build(EDIT_ASK[v]))
    if v == "edit_guests":
        _set(c, nk_step=v)
        return c.send(line.text("お客様の人数を数字で教えてください。"))
    if v == "edit_guide":
        _set(c, nk_step=v)
        return c.send(line.text("当日ご案内する担当者のお名前を教えてください。ご自身がご案内する場合は「本人」と送ってください。"))
    if v == "again":
        fid = c.u.get("last_naiken")
        rec = store.db().get("faxes", fid) if fid else None
        d = dict(rec["draft"]) if rec else {}
        d["wishes"] = []
        _set(c, nk=d, nk_step="dates")
        return c.send(texts.build("naiken_ask_dates"))


def edit_card(c):
    _set(c, nk={"card_only": True}, nk_step="card_name")
    c.send(ask_card("card_name"))


def cancel(c):
    _set(c, nk={}, nk_step=None, nk_fid=None)
    c.send(texts.build("naiken_canceled"))
