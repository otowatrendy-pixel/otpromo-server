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


class _Words:
    """売買の依頼書（下書きの kind が sale）を扱っている間は、送る文面の言い方を売買用にする。
    賃貸の内見申込書の会話はそのまま。送る瞬間の下書きで決めるので、途中で賃貸・売買が入れ替わっても正しく出る。"""

    def __init__(self, c):
        self.__dict__["_c"] = c

    def __getattr__(self, k):
        return getattr(self._c, k)

    def __setattr__(self, k, v):
        setattr(self._c, k, v)

    def send(self, *messages):
        if ((self._c.u or {}).get("nk") or {}).get("kind") == "sale":
            messages = [naiken.sale_words(m) for m in messages]
        return self._c.send(*messages)


def _words(fn):
    import functools

    @functools.wraps(fn)
    def run(c, *a, **kw):
        return fn(c if isinstance(c, _Words) else _Words(c), *a, **kw)
    return run

ASK_ONLY = {"prop": "物件名・号室・所在地", "dates": "内見の希望日時", "to": "管理会社の名前とFAX番号",
            "fax_invalid": "管理会社の名前とFAX番号", "edit_to": "管理会社の名前とFAX番号",
            "edit_fax": "管理会社のFAX番号", "edit_prop": "物件名・号室・所在地", "edit_dates": "内見の希望日時"}


def active(u):
    return bool(u.get("nk_step"))


def _set(c, **kv):
    store.update_user(c.uid, kv)
    c.refresh()


def trial_message(c):
    import bot
    return texts.build("naiken_trial", urls=bot.plan_urls(c))


def start(c):
    if naiken.is_trial(c.u):
        return c.send(trial_message(c))
    _set(c, nk={}, nk_step="free", nk_fid=None)
    c.send(texts.build("naiken_start"))


@_words
def start_from_job(c, jid):
    if naiken.is_trial(c.u):
        return c.send(trial_message(c))
    job = store.get_job(jid) if jid else None
    if not job or job.get("status") != "done" or job.get("user_id") != c.uid:
        return c.send(line.text("お作りした資料が見つかりませんでした。物件名と号室を教えてください。"),
                      texts.build("naiken_ask_property"))
    job["_id"] = jid
    d = naiken.from_job(job)
    to = d["to"]
    p = d.get("prop") or {}
    if to.get("company") and naiken.fax_ok(to.get("fax")) and to.get("check"):
        # 読み取りに自信がないときは、送る前に宛先を確かめてもらう
        _set(c, nk=d, nk_step="to")
        return c.send(texts.build("naiken_fax_unread", text=(
            f"宛先を「{to['company']}　FAX {naiken.fax_display(to['fax'])}」と読み取りましたが、"
            "読み取りに自信がありません。\n誤送信を防ぐため、管理会社の名前とFAX番号を、募集図面を見ながらもう一度送ってください。")))
    if not to.get("fax") and not ((job.get("texts") or {}).get("motozuke") or {}).get("fax"):
        # 募集図面にFAX番号の記載がない（精度検証B）。回数は使わない。番号が分かれば送ってもらえば続けられる
        _set(c, nk=d, nk_step="to")
        return c.send(line.text("この募集図面には、元付業者のFAX番号の記載がありません。"
                                "内見のお申し込みは、元付業者に直接ご確認ください。\n\n"
                                "FAX番号がお分かりでしたら、管理会社の名前とFAX番号をこのトークに送っていただければ、"
                                "内見申込書をお送りします。"))
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


@_words
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
    text = text + "\n日時を指定して送る場合は「日時を指定して送る」から（8時〜20時・7日先まで）。"
    msg = with_picker(texts.build("naiken_confirm", text=text), "naiken=reserve")
    c.send(*pre, img, msg)


# ---------- 送信予約 ----------

RESERVED_WORDS = {"予約", "予約中", "予約確認", "予約中のFAX", "FAX予約", "予約一覧"}


def picker(label, data):
    """LINEの日時選択ボタン（15分後〜7日後）。選ぶと postback に &dt=YYYY-MM-DDTHH:MM が付いて届く。"""
    now = store.now().astimezone(store.JST)
    first = now + naiken.RESERVE_MIN + timedelta(minutes=5)
    if first.hour < naiken.RESERVE_HOURS[0]:
        first = first.replace(hour=naiken.RESERVE_HOURS[0], minute=0)
    elif first.hour >= naiken.RESERVE_HOURS[1]:
        first = (first + timedelta(days=1)).replace(hour=naiken.RESERVE_HOURS[0], minute=0)
    f = "%Y-%m-%dT%H:%M"
    return {"type": "datetimepicker", "label": label[:20], "data": data, "mode": "datetime",
            "initial": first.strftime(f), "min": (now + naiken.RESERVE_MIN).strftime(f),
            "max": (now + naiken.RESERVE_MAX).strftime(f)}


def with_picker(msg, data, label="日時を指定して送る"):
    """クイックリプライの2番目（「この内容で送信」の次）に日時選択のボタンを入れる。"""
    items = msg.setdefault("quickReply", {"items": []})["items"]
    items.insert(min(1, len(items)), {"type": "action", "action": picker(label, data)})
    return msg


def _split_dt(v):
    """「reserve&dt=2026-11-01T10:00」→ ("reserve", datetime)"""
    head, _, dt = v.partition("&dt=")
    return head, naiken.parse_when(dt) if dt else None


def reserved_text(fid, rec):
    d = rec["draft"]
    to, p = d.get("to") or {}, d.get("prop") or {}
    room = f" {p['room']}号室" if p.get("room") else ""
    return (f"送信日時：{naiken.when_label(rec['send_at'])}\n送り先：{to_text(to)}（{naiken.fax_display(to.get('fax'))}）\n"
            f"物件：{p.get('name', '')}{room}")


def reserved_buttons(fid):
    return [{"type": "action", "action": picker("日時を変える", f"naiken=resched:{fid}")},
            line.qr_postback("予約を取り消す", f"naiken=unreserve:{fid}"),
            line.qr_postback("予約中のFAX", "naiken=reserved")]


def reserve(c, when):
    fid = c.u.get("nk_fid")
    if not fid:
        return c.send(line.text("送る内容が見つかりませんでした。もう一度はじめからお願いします。"))
    status = naiken.reserve(c.uid, fid, when)
    rec = store.db().get("faxes", fid)
    if status == "scheduled":
        _set(c, nk_step=None, last_naiken=fid)
        t = ("内見申込書のFAXを、送信予約しました。\n\n" + reserved_text(fid, rec) +
             "\n\nこの時刻に自動でお送りし、届いたらこちらでお知らせします。"
             "今月の内見FAXの回数は、予約した時点で1回分使い、取り消すと戻ります。\n"
             "取り消し・日時の変更は、送信の5分前までできます（「予約」と送ると、予約中のFAXが見られます）。")
        note = naiken.holiday_note(rec["send_at"])
        return c.send(line.text(t + ("\n\n" + note if note else ""), quick=reserved_buttons(fid)))
    if status in naiken.RESERVE_ERRORS:
        q = [{"type": "action", "action": picker("日時を選び直す", "naiken=reserve")},
             line.qr_postback("今すぐ送る", "naiken=send"), line.qr_postback("やめる", "cancel")]
        return c.send(line.text(naiken.error_text(status, rec and rec["draft"]), quick=q))
    return answer(c, fid, status, rec)


@_words
def show_reserved(c):
    rows = naiken.reserved(c.uid)
    if not rows:
        return c.send(line.text("予約中の内見申込書FAXはありません。"))
    msgs = [line.text(f"予約中の内見申込書FAXは{len(rows)}件です（送信の早い順）。\n取り消し・日時の変更は、送信の5分前までできます。")]
    for fid, rec in rows[:4]:  # 1回で送れるのは5通まで
        msgs.append(line.text(reserved_text(fid, rec), quick=reserved_buttons(fid)))
    if len(rows) > 4:
        msgs[0]["text"] += f"\n（ここには早い順に4件を出しています）"
    return c.send(*msgs)


def reserve_postback(c, v):
    head, when = _split_dt(v)
    if head == "reserve":
        return reserve(c, when)
    if head == "reserved":
        return show_reserved(c)
    if head.startswith("unreserve:"):
        st = naiken.unreserve(c.uid, head.split(":", 1)[1])
        if st == "canceled":
            return c.send(line.text("送信予約を取り消しました。今回の分は、回数に戻しています。\n"
                                    "あらためて送る場合は、その物件の資料の「この物件で内見FAX」からお願いします。"))
        return c.send(line.text(naiken.RESERVE_ERRORS.get(st or "not_reserved")))
    if head.startswith("resched:"):
        fid = head.split(":", 1)[1]
        st = naiken.reschedule(c.uid, fid, when)
        rec = store.db().get("faxes", fid)
        if st == "scheduled":
            note = naiken.holiday_note(rec["send_at"])
            return c.send(line.text("送信日時を変更しました。\n\n" + reserved_text(fid, rec) + ("\n\n" + note if note else ""),
                                    quick=reserved_buttons(fid)))
        q = reserved_buttons(fid) if st not in ("too_late", "not_reserved", None) else None
        return c.send(line.text(naiken.error_text(st or "not_reserved", rec and rec["draft"]), quick=q))


# ---------- 名刺の情報 ----------

CARD_ASK = {
    "card_name": "naiken_card_intro", "card_roman": "naiken_card_roman", "card_role": "naiken_card_role",
    "card_contact": "naiken_card_contact", "card_fax": "naiken_card_fax",
}


def ask_card(step, first=False):
    if step == "card_name":
        return line.text("ご担当者様のお名前を、姓・名のフルネームで教えてください。\n（例：山田 花子）")
    if step == "card_roman" and first:
        return texts.build("naiken_card_intro")  # はじめての内見FAX：あいさつとローマ字をまとめて聞く
    return texts.build(CARD_ASK[step])


def card_text(c, step, t, co):
    none = t in ("なし", "無し", "ない")
    t = unicodedata.normalize("NFKC", t).strip() if step != "card_name" else t.strip()
    if step == "card_name":
        import person
        if person.try_name(c.uid, c.u, t) == "retry":
            return c.send(texts.build("reg_staff_retry"))
        co = dict(store.get_user(c.uid).get("company") or {})
    elif step == "card_roman":
        co["ローマ字"] = "" if none else t[:40]
    elif step == "card_role":
        co["役職"] = "" if none else t[:40]
    elif step == "card_contact":
        if not none:
            # 携帯は登録で必須（ここではメールだけ聞く）。書いてあれば携帯も直す
            import person
            e = re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", t)
            co["メール"] = e.group() if e else ""
            if person.normalize_mobile(t):
                co["携帯"] = person.normalize_mobile(t)
        else:
            co["メール"] = ""
    elif step == "card_fax":
        if not naiken.fax_ok(t):
            return c.send(texts.build("naiken_card_fax"))
        co["FAX"] = naiken.fax_display(t)
    store.update_user(c.uid, {"company": co})
    c.refresh()
    card_next(c, step)


@_words
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


@_words
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
        return answer(c, fid, status, rec)
    if v.split("&")[0] in ("reserve", "reserved") or v.startswith(("unreserve:", "resched:")):
        return reserve_postback(c, v)
    if v == "edit":
        return c.send(texts.build("naiken_edit"))
    return answer_rest(c, v)


def answer(c, fid, status, rec):
    """「この内容で送信」「日時を指定して送る」の結果を伝える（送れなかった理由ごと）。"""
    if status == "sending":
        _set(c, nk_step=None, last_naiken=fid)
        return c.send(texts.build("naiken_sending"))
    if status == "trial":
        _set(c, nk_step=None)
        return c.send(trial_message(c))
    if status == "limit":
        import bot
        period, limit, nxt = naiken.limit_info(c.u)
        if period == "本日":
            return c.send(texts.build("naiken_limit_day", 上限=limit))
        return c.send(texts.build("naiken_limit", urls=bot.plan_urls(c), 上限=limit, 更新日=nxt))
    if status == "fax_invalid":
        return next_step(c)
    if status == "optout":
        _set(c, nk_step=None, last_naiken=fid)
        return c.send(_optout_message(fid, rec))
    if status == "not_ready":
        _set(c, nk_step=None, last_naiken=fid)
        return c.send(line.text("ただいまFAX送信の準備中のため、まだお送りしていません（テスト版）。\n"
                                f"作った申込書はこちらから見られます。\n{naiken.file_url(fid, rec, 'naiken.pdf')}"))
    if status == "scheduled":
        return c.send(line.text("この申込書は、送信予約をしています。今すぐ送る場合は、予約を取り消してから「この内容で送信」を押してください。\n\n"
                                + reserved_text(fid, rec), quick=reserved_buttons(fid)))
    return c.send(line.text("この申込書は、すでに送信の手続きが済んでいます。"))


def answer_rest(c, v):
    """確認カードの「直す」から先のボタン。"""
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


@_words
def edit_card(c):
    first = "card_roman" if (c.u.get("company") or {}).get("担当者名") else "card_name"
    _set(c, nk={"card_only": True}, nk_step=first)
    c.send(ask_card(first))


@_words
def cancel(c):
    _set(c, nk={}, nk_step=None, nk_fid=None)
    c.send(texts.build("naiken_canceled"))


def _optout_message(fid, rec):
    """送信停止リストの番号だったとき（10/29本人承認）。申込書は作ってあるので、電話などで直接の連絡をお願いする。"""
    to = ((rec or {}).get("draft") or {}).get("to") or {}
    if "naiken_optout" in texts._MSG:
        return texts.build("naiken_optout", 管理会社名=to.get("company", ""), FAX番号=to.get("fax", ""))
    return line.text(f"申し訳ありません。{to.get('company') or 'この管理会社'}さま（FAX {to.get('fax', '')}）から、"
                     "1分資料からのFAXを止めてほしいとのご依頼をいただいているため、お送りできません。\n"
                     "お手数ですが、お電話など直接ご連絡をお願いいたします。\n"
                     f"作った申込書はこちらから見られます。\n{naiken.file_url(fid, rec, 'naiken.pdf')}")
