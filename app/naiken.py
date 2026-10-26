"""内見申込書FAX。LINEの会話とアプリのAPIの両方から使う。

流れ：入力（自由な文 or 直前のマイソク）→ AIで項目に分ける → 足りない項目を聞く
      → 名刺の情報（初回だけ）→ 確認カード（申込書の画像つき）→［この内容で送信］でだけ送る
"""
import json
import logging
import re
import secrets
import subprocess
import tempfile
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import config
import delivery
import render
import store
from schema import Facts, Result

log = logging.getLogger("naiken")
WEEK = "月火水木金土日"

# 送信回数の上限（設定で変えられる）。plan → (月, 日)
LIMITS = {
    "free": (int(config.env("NAIKEN_FREE_MONTH", "2")), int(config.env("NAIKEN_DAY", "20"))),
    "light": (int(config.env("NAIKEN_LIGHT_MONTH", "20")), int(config.env("NAIKEN_DAY", "20"))),
    "unlimited": (int(config.env("NAIKEN_UNLIMITED_MONTH", "100")), int(config.env("NAIKEN_DAY", "20"))),
}


# ---------- FAX番号 ----------

def normalize_fax(s):
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[^\d]", "", s)


def fax_ok(s):
    """国内の固定電話のFAX番号だけ通す。携帯・フリーダイヤル・ナビダイヤル・国際番号などは不可。"""
    raw = unicodedata.normalize("NFKC", s or "").strip()
    if raw.startswith("+") or raw.startswith("00") or raw.startswith("010"):
        return False
    d = normalize_fax(raw)
    if len(d) != 10 or not d.startswith("0"):
        return False
    if d[:4] in ("0120", "0800", "0570", "0990", "0180", "0170") or d[:3] in ("050", "020", "060", "070", "080", "090"):
        return False
    return True


def fax_display(s):
    d = normalize_fax(s)
    if len(d) != 10:
        return s or ""
    if d.startswith("03") or d.startswith("06"):
        return f"{d[:2]}-{d[2:6]}-{d[6:]}"
    return f"{d[:3]}-{d[3:6]}-{d[6:]}"


# ---------- 日時の表示 ----------

def wish_label(w):
    """{"date": "2026-10-29", "time": "14:00", "note": "午前"} → 10月29日（木）14時00分頃"""
    try:
        d = datetime.strptime(w["date"], "%Y-%m-%d")
    except (KeyError, ValueError, TypeError):
        return w.get("text") or ""
    s = f"{d.month}月{d.day}日（{WEEK[d.weekday()]}）"
    if w.get("time"):
        h, m = w["time"].split(":")[:2]
        s += f"{int(h)}時{m}分頃"
    elif w.get("note"):
        s += w["note"]
    return s


# ---------- 自由な文をAIで項目に分ける ----------

PARSE_SYSTEM = """不動産会社の営業担当者が、管理会社へ送る内見申込書FAXの内容をLINEに書いてきます。
文から項目を取り出し、JSONだけを返します。前後に説明やコードブロックは付けません。
書いていない項目は null にします。推測で埋めません。

{
 "prop_name": 物件名（号室は含めない）, "room": 号室の数字や記号, "address": 所在地,
 "to_company": 管理会社・元付業者の名前, "to_branch": 部署・支店, "to_fax": FAX番号（書いてあるまま）,
 "wishes": [{"date": "YYYY-MM-DD", "time": "HH:MM" または null, "note": "午前" "午後" など時刻がないときの言い方 または null}]（最大3つ、書いてある順）,
 "guests": お客様の人数（整数）, "guide": 当日ご案内する担当者の名前（本人以外と書いてあるときだけ）
}

日付は、今の日時（日本時間）をもとに、「明日」「土曜」「10/29」などを実際の日付に直します。
年が書いていなければ、今日以降で一番近い日にします。"""


def ai_parse(text, only=None):
    import anthropic
    now = datetime.now(store.JST)
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_KEY, base_url="https://api.anthropic.com")
    hint = f"\n今回は「{only}」の項目だけを答えてください。ほかは null にします。" if only else ""
    resp = client.messages.create(
        model=config.env("NAIKEN_MODEL", "claude-haiku-5-5"), max_tokens=1500,
        system=PARSE_SYSTEM + hint,
        messages=[{"role": "user", "content": f"今の日時：{now:%Y-%m-%d %H:%M}（{WEEK[now.weekday()]}曜日）\n\n{text}"}],
    )
    t = "".join(b.text for b in resp.content if b.type == "text")
    obj, _ = json.JSONDecoder().raw_decode(t[t.index("{"):])
    return obj


def merge(draft, parsed):
    """AIの結果を下書きに足す（null は上書きしない）。"""
    d = json.loads(json.dumps(draft or {}, default=str))
    d.setdefault("to", {})
    d.setdefault("prop", {})
    for k, (sec, key) in {"prop_name": ("prop", "name"), "room": ("prop", "room"), "address": ("prop", "address"),
                          "to_company": ("to", "company"), "to_branch": ("to", "branch"),
                          "to_fax": ("to", "fax")}.items():
        if parsed.get(k):
            d[sec][key] = str(parsed[k]).strip()
            if k == "to_fax":
                d["to"]["from_maisoku"] = False
    if parsed.get("wishes"):
        d["wishes"] = parsed["wishes"][:3]
    if parsed.get("guests"):
        d["guests"] = int(parsed["guests"])
    if parsed.get("guide"):
        d["guide"] = parsed["guide"]
    return d


def from_job(job):
    """直前のマイソクから下書きを作る。"""
    r = Result.model_validate(job["result"]) if job.get("result") else None
    f = r.事実 if r else Facts(取引="賃貸")
    name, room = split_room(f.物件名 or job.get("title") or "")
    return {"job_id": job.get("_id"), "prop": {"name": name, "room": room, "address": f.所在地 or ""},
            "to": {"company": f.元付業者 or "", "branch": "", "fax": f.元付業者_FAX or "",
                   "from_maisoku": bool(f.元付業者_FAX)},
            "wishes": [], "guests": None, "guide": None}


def split_room(name):
    m = re.search(r"\s*(\d+)\s*号室?$", name or "")
    if m:
        return name[:m.start()].strip(), m.group(1)
    return name, None


# ---------- 足りない項目 ----------

def missing(d):
    """次に聞くべき項目。そろっていれば None。"""
    if not (d.get("prop") or {}).get("name"):
        return "prop"
    if not d.get("wishes"):
        return "dates"
    to = d.get("to") or {}
    if not to.get("company") or not to.get("fax"):
        return "to"
    if not fax_ok(to["fax"]):
        return "fax_invalid"
    return None


CARD_STEPS = ["card_name", "card_roman", "card_role", "card_contact", "card_fax"]


def card_missing(company):
    """名刺の情報で、まだ聞いていない項目。"""
    if company.get("名刺済み"):
        return [] if company.get("FAX") else ["card_fax"]
    steps = ["card_name", "card_roman", "card_role", "card_contact"]
    if not company.get("FAX"):
        steps.append("card_fax")
    return steps


# ---------- 申込書を作る ----------

def me_from_company(co, guide=None):
    name = co.get("担当者名", "")
    return {"company": co.get("会社名", ""), "branch": co.get("店舗名", ""), "role": co.get("役職", ""),
            "name": name, "family_name": (guide or name.split(" ")[0].split("　")[0] or "担当者"),
            "roman": co.get("ローマ字", ""), "zip": co.get("郵便番号", ""), "address": co.get("住所", ""),
            "tel": co.get("電話", ""), "fax": co.get("FAX", ""), "mobile": co.get("携帯", ""),
            "email": co.get("メール", ""), "license": co.get("免許", "")}


def build(d, company):
    """下書きと会社情報から、申込書のPDFとプレビュー画像（PNG）を作る。"""
    to = dict(d.get("to") or {})
    to["fax"] = fax_display(to.get("fax", ""))
    to.setdefault("person", "")
    prop = d.get("prop") or {}
    r = Result(事実=Facts(取引="賃貸"))
    html_text = render.naiken_html(r, company, wishes=[wish_label(w) for w in d.get("wishes", [])],
                                   to=to, me=me_from_company(company, d.get("guide")), guests=d.get("guests"),
                                   prop={"name": prop.get("name", ""), "room": prop.get("room"),
                                         "address": prop.get("address", "")})
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "naiken.pdf"
        render.to_pdf(html_text, pdf)
        png = Path(tmp) / "naiken"
        subprocess.run(["pdftoppm", "-r", "110", "-png", "-singlefile", str(pdf), str(png)], check=True)
        return pdf.read_bytes(), (Path(tmp) / "naiken.png").read_bytes()


# ---------- 回数 ----------

def usage(u):
    now = datetime.now(store.JST)
    c = u.get("naiken_count") or {}
    month = c.get("m", {}).get(now.strftime("%Y-%m"), 0)
    day = c.get("d", {}).get(now.strftime("%Y-%m-%d"), 0)
    return month, day


def can_send(u):
    m, d = usage(u)
    lim_m, lim_d = LIMITS.get(u.get("plan") or "free", LIMITS["free"])
    return m < lim_m and d < lim_d


def limit_info(u):
    """上限に達したときの案内用。(期間, 上限, 次に使える日)"""
    m, d = usage(u)
    lim_m, lim_d = LIMITS.get(u.get("plan") or "free", LIMITS["free"])
    if d >= lim_d and m < lim_m:
        return "本日", lim_d, "明日"
    now = datetime.now(store.JST)
    nxt = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)
    return "今月", lim_m, f"{nxt.month}月{nxt.day}日"


def count(uid, delta=1):
    now = datetime.now(store.JST)
    mk, dk = now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")

    def fn(u):
        c = u.get("naiken_count") or {}
        c = {"m": {mk: c.get("m", {}).get(mk, 0) + delta}, "d": {dk: c.get("d", {}).get(dk, 0) + delta}}
        u["naiken_count"] = c
    store.db().update_tx("users", uid, fn)


# ---------- 送る ----------

def save_preview(uid, d, company):
    """プレビューを作って保存する。戻り値 (fax_id, rec)。"""
    pdf, png = build(d, company)
    fid = "N" + store.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
    rec = {"user_id": uid, "status": "preview", "draft": d, "token": secrets.token_urlsafe(24),
           "expires_at": store.now() + timedelta(days=7), "created_at": store.now()}
    store.db().set("faxes", fid, rec)
    store.db().put_file(f"faxes/{fid}/naiken.pdf", pdf, "application/pdf")
    store.db().put_file(f"faxes/{fid}/naiken.png", png, "image/png")
    return fid, rec


def file_url(fid, rec, name):
    return f"{config.BASE_URL}/n/{fid}/{rec['token']}/{name}"


def read_file(fid, token, name):
    if name not in ("naiken.pdf", "naiken.png"):
        return None
    rec = store.db().get("faxes", fid)
    if not rec or rec.get("token") != token:
        return None
    if rec["expires_at"] < store.now():
        return "expired"
    return store.db().get_file(f"faxes/{fid}/{name}")


def send(uid, fid):
    """［この内容で送信］が押されたときだけ呼ぶ。戻り値は status。"""
    rec = store.db().get("faxes", fid)
    if not rec or rec["user_id"] != uid or rec["status"] not in ("preview", "failed"):
        return rec and rec["status"]
    u = store.get_user(uid)
    if not can_send(u):
        return "limit"
    to = rec["draft"]["to"]
    if not fax_ok(to.get("fax")):
        return "fax_invalid"
    pdf = store.db().get_file(f"faxes/{fid}/naiken.pdf")
    count(uid, 1)
    ok = delivery.fax_ready() and delivery.send_fax(normalize_fax(to["fax"]), pdf)
    status = "sending" if ok else ("failed" if delivery.fax_ready() else "not_ready")
    if not ok:
        count(uid, -1)
    store.db().set("faxes", fid, {"status": status, "sent_at": store.now()}, merge=True)
    if ok:
        remember_to(uid, to)
        store.db().update_tx("users", uid, lambda u: u.__setitem__("total_naiken", u.get("total_naiken", 0) + 1))
    return status


def remember_to(uid, to):
    def fn(u):
        h = [x for x in (u.get("naiken_history") or []) if normalize_fax(x.get("fax")) != normalize_fax(to.get("fax"))]
        u["naiken_history"] = ([{"company": to.get("company", ""), "branch": to.get("branch", ""),
                                 "fax": to.get("fax", "")}] + h)[:10]
    store.db().update_tx("users", uid, fn)
