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
# 無料お試し（最初の5件）の間は内見申込書FAXを使えない（10/28本人決定、正式入会を促す）。
# モニター（ライトの無料期間）と店舗まとめ割のメンバーは plan が light/unlimited なので使える
LIMITS = {
    "free": (int(config.env("NAIKEN_FREE_MONTH", "0")), int(config.env("NAIKEN_DAY", "20"))),
    # 売買版はライト10回・MAX30回（10/31本人決定）
    "light": (int(config.env("NAIKEN_LIGHT_MONTH", "10" if config.APP_MODE == "sale" else "20")), int(config.env("NAIKEN_DAY", "20"))),
    "unlimited": (int(config.env("NAIKEN_UNLIMITED_MONTH", "30" if config.APP_MODE == "sale" else "50")), int(config.env("NAIKEN_DAY", "20"))),
}
PACK_SIZE = int(config.env("FAX_PACK_SIZE", "10"))     # 追加FAX：10回500円
PACK_DAYS = int(config.env("FAX_PACK_DAYS", "90"))     # 購入から3か月有効（月末にリセットしない）


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
        return ""  # 日付として読めないものは載せない（利用者の自由な文をそのままFAXに載せないため）
    s = f"{d.month}月{d.day}日（{WEEK[d.weekday()]}）"
    t = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(w.get("time") or ""))
    if t and int(t.group(1)) < 24 and int(t.group(2)) < 60:
        s += f"{int(t.group(1))}時{t.group(2)}分頃"
    elif str(w.get("note") or "").strip() in WISH_NOTES:
        s += str(w["note"]).strip()
    return s


# 時刻がないときに載せてよい言い方（決まった言葉だけ。自由な文はFAXに載せない）
WISH_NOTES = {"午前", "午後", "夕方", "終日", "午前中", "午後以降", "10時〜12時", "13時〜15時", "15時〜17時"}


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
    if job.get("kind") == "sale":
        return from_sale_job(job)
    r = Result.model_validate(job["result"]) if job.get("result") else None
    f = r.事実 if r else Facts(取引="賃貸")
    name, room = split_room(f.物件名 or job.get("title") or "")
    if f.号室:
        room = re.sub(r"\s*号室?$", "", f.号室.strip()) or room
    # 読み取りに自信がない・空欄の宛先項目（確認か入力が済むまで送らない）
    shaky = set(f.読み取りに自信がない項目)
    check = [k for k, field, v in (("company", "元付業者", f.元付業者), ("fax", "元付業者_FAX", f.元付業者_FAX))
             if field in shaky or not v]
    return {"job_id": job.get("_id"), "prop": {"name": name, "room": room, "address": f.所在地 or ""},
            "to": {"company": f.元付業者 or "", "branch": "", "fax": f.元付業者_FAX or "",
                   "from_maisoku": bool(f.元付業者_FAX), "check": check},
            "wishes": [], "guests": None, "guide": None}


def from_sale_job(job):
    """売買の資料から「内見依頼書」の下書きを作る（中身は baibai_naiken.from_sale_facts とそろえる）。"""
    import baibai_naiken
    f = (job.get("result") or {}).get("事実") or {}
    d = baibai_naiken.from_sale_facts(f)
    p = d["prop"]
    if not p.get("name"):  # 土地などで物件名がないときは所在地で呼ぶ
        p["name"] = p.get("address") or job.get("title") or ""
    return {"kind": "sale", "job_id": job.get("_id"), "prop": p,
            "to": {**d["to"], "from_maisoku": bool(d["to"].get("fax"))},
            "wishes": [], "guests": None, "guide": None}


# 売買の依頼書のときは、賃貸の内見申込書の文面の言い方を売買に合わせる
SALE_WORDS = [("内見申込書", "内見依頼書"), ("内見申込", "内見依頼"), ("管理会社", "元付業者"), ("募集図面", "販売図面")]
SALE_WORDS_SHORT = SALE_WORDS  # ボタン（20文字まで）


def sale_words(x, short=False):
    """文面（LINEのメッセージの dict・文字列）の言い方を売買用に置き換える。"""
    if isinstance(x, str):
        for a, b in (SALE_WORDS_SHORT if short else SALE_WORDS):
            x = x.replace(a, b)
        return x
    if isinstance(x, list):
        return [sale_words(v, short) for v in x]
    if isinstance(x, dict):
        return {k: (sale_words(v, short or k in ("label", "displayText")) if k not in ("data", "uri", "type") else v)
                for k, v in x.items()}
    return x


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


CARD_STEPS = ["card_name", "card_roman", "card_role", "card_contact", "card_fax"]  # card_name は以前の方だけ


def card_missing(company):
    """名刺の情報で、まだ聞いていない項目。"""
    if company.get("名刺済み"):
        return [] if company.get("FAX") else ["card_fax"]
    # 担当者名と携帯は初回登録で必須（10/28）。名前がまだない以前の方だけ名前から聞く
    steps = ([] if company.get("担当者名") else ["card_name"]) + ["card_roman", "card_role", "card_contact"]
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


def build(d, company, fax=False):
    """下書きと会社情報から、申込書のPDFとプレビュー画像（PNG）を作る。
    fax=True は売買の内見依頼書のFAX送信用（文字・線・帯をすべて黒）。プレビュー・控えは fax=False（青のまま）。"""
    to = dict(d.get("to") or {})
    to["fax"] = fax_display(to.get("fax", ""))
    to.setdefault("person", "")
    prop = d.get("prop") or {}
    if d.get("kind") == "sale":
        import baibai_naiken
        html_text = baibai_naiken.html(to=to, me=me_from_company(company, d.get("guide")), prop=prop,
                                       wishes=[wish_label(w) for w in d.get("wishes", [])],
                                       fax=fax, **_sale_qr_kw(company))
        return _pdf_png(html_text)
    r = Result(事実=Facts(取引="賃貸"))
    html_text = render.naiken_html(r, company, wishes=[wish_label(w) for w in d.get("wishes", [])],
                                   to=to, me=me_from_company(company, d.get("guide")), guests=d.get("guests"),
                                   prop={"name": prop.get("name", ""), "room": prop.get("room"),
                                         "address": prop.get("address", "")})
    return _pdf_png(html_text)


def _sale_qr_kw(company):
    """会社の設定で、QRと一言を載せるか。載せないときは空にする。"""
    import sale_bot
    if not sale_bot.naiken_qr_on(company):
        return {"qr_svg": "", "qr_caption": ""}
    qr = _sale_qr()
    return {"qr_svg": qr} if qr else {}


def _sale_qr():
    """内見依頼書のQR（static/naiken-qr-sale.svg、中身は短い https://otpromo.com/b/fax で25マス。FAXの標準画質でも読めるように）。
    HP側で https://otpromo.com/ippun-baibai.html?utm_source=naiken_fax へ転送する
    （10/31集客スレッドの決定。友だち追加URLではない）。無ければ None（baibai_naiken 側の既定を使う）。"""
    p = Path(__file__).parent / "static" / "naiken-qr-sale.svg"
    if not p.exists():
        return None
    from markupsafe import Markup
    return Markup(p.read_text(encoding="utf-8"))


def _pdf_png(html_text):
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
    month = c.get("m", {}).get(store.period_key(u), 0)
    day = c.get("d", {}).get(now.strftime("%Y-%m-%d"), 0)
    return month, day


def pack_left(u):
    t = store.now()
    return sum(p["n"] - p["used"] for p in (u.get("fax_packs") or []) if p["exp"] > t)


def is_trial(u):
    """有料プランの契約がない（無料お試し中）なら True。"""
    return (u.get("plan") or "free") == "free"


def month_limit(u):
    """今月の内見FAXの上限。モニターからカード登録で続けた方は、最初の有料期間だけ＋10回。"""
    import monitor
    return LIMITS.get(u.get("plan") or "free", LIMITS["free"])[0] + monitor.bonus_sheets(u)


def remaining(u):
    """(今月の残り, 追加購入分の残り, 本日の残り)"""
    m, d = usage(u)
    lim_m, lim_d = month_limit(u), LIMITS.get(u.get("plan") or "free", LIMITS["free"])[1]
    return max(0, lim_m - m), pack_left(u), max(0, lim_d - d)


def can_send(u):
    month, packs, day = remaining(u)
    return day > 0 and (month > 0 or packs > 0)


def limit_info(u):
    """上限に達したときの案内用。(期間, 上限, 次に使える日)"""
    month, packs, day = remaining(u)
    lim_m, lim_d = month_limit(u), LIMITS.get(u.get("plan") or "free", LIMITS["free"])[1]
    if day <= 0 and (month > 0 or packs > 0):
        return "本日", lim_d, "明日"
    return "今月", lim_m, store.renew_date(u)


def count(uid, delta=1, source=None):
    """送信1回分を数える。今月の枠を先に使い、なくなったら追加購入分から使う。delta=-1 で取り消し。
    source は取り消すときに、どちらから使った分か（"month"・"pack"、送った時に faxes に残してある）。"""
    now = datetime.now(store.JST)
    dk = now.strftime("%Y-%m-%d")

    def fn(u):
        # 月の区切りは資料の件数と同じ（有料は申込日と同じ日ごと、お試しは暦の月）
        mk = store.period_key(u)
        c = u.get("naiken_count") or {}
        m = c.get("m", {}).get(mk, 0)
        lim_m = month_limit(u)
        if delta > 0 and m >= lim_m:
            # 今月の枠を使い切っている → 期限の近い追加購入分から使う
            packs = sorted([p for p in (u.get("fax_packs") or []) if p["exp"] > store.now() and p["used"] < p["n"]],
                           key=lambda p: p["exp"])
            if packs:
                packs[0]["used"] += 1
                u["last_fax_from"] = "pack"
        elif delta > 0:
            u["last_fax_from"] = "month"
        if delta < 0 and (source or u.get("last_fax_from")) == "pack":
            used = sorted([p for p in (u.get("fax_packs") or []) if p["used"] > 0], key=lambda p: p["exp"])
            if used:
                used[0]["used"] -= 1
        elif not (delta > 0 and m >= lim_m):
            m = m + delta
        u["naiken_count"] = {"m": {mk: max(0, m)}, "d": {dk: max(0, c.get("d", {}).get(dk, 0) + delta)}}
    store.db().update_tx("users", uid, fn)


def add_pack(uid, n=PACK_SIZE):
    def fn(u):
        u["fax_packs"] = [p for p in (u.get("fax_packs") or []) if p["exp"] > store.now()] + [
            {"n": n, "used": 0, "at": store.now(), "exp": store.now() + timedelta(days=PACK_DAYS)}]
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
    if d.get("kind") == "sale":  # 売買はFAXで送る版（真っ黒）を別に作っておく。送るときはこちらを使う
        store.db().put_file(f"faxes/{fid}/naiken_fax.pdf", build(d, company, fax=True)[0], "application/pdf")
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
    if is_trial(u):
        return "trial"
    if not can_send(u):
        return "limit"
    to = rec["draft"]["to"]
    if not fax_ok(to.get("fax")):
        return "fax_invalid"
    if opted_out(to.get("fax")):
        # 元付業者さんから「FAXを止めて」と言われた番号には送らない（10/29本人承認）
        store.db().set("faxes", fid, {"status": "optout"}, merge=True)
        return "optout"
    count(uid, 1)
    source = (store.get_user(uid) or {}).get("last_fax_from")
    return _dispatch(uid, fid, rec, u, source)


def _dispatch(uid, fid, rec, u, source):
    """回数を数えた後、faximo へ渡す。送れなければ回数を戻す。今すぐ送る・予約の時刻の両方から使う。"""
    to = rec["draft"]["to"]
    pdf = store.db().get_file(f"faxes/{fid}/naiken_fax.pdf") or store.db().get_file(f"faxes/{fid}/naiken.pdf")
    sender = delivery.fax_sender((u.get("company") or {}).get("会社名"))
    ready = delivery.fax_ready() or bool(u.get("review"))  # 審査用アカウントは送ったことにする
    ok = ready and delivery.send_fax(normalize_fax(to["fax"]), pdf, sender,
                                                    kind="naiken", ref=fid, uid=uid)
    status = "sending" if ok else ("failed" if ready else "not_ready")
    if not ok:
        count(uid, -1, source=source)
    store.db().set("faxes", fid, {"status": status, "sent_at": store.now(), "fax_id": ok or None,
                                  "from": source, "retry_told": False}, merge=True)
    if ok:
        remember_to(uid, to)
        store.db().update_tx("users", uid, lambda u: u.__setitem__("total_naiken", u.get("total_naiken", 0) + 1))
    return status


# ---------- 送信予約（10/29本人決定：公開日から） ----------
# faximo には予約送信がないため、サーバーで預かり、定期実行（5分ごと）が時刻になったものを送る。
# 回数は予約した時点で1回分を押さえ、取り消したら戻す。送れなかった分は今すぐ送るときと同じく数えない。

RESERVE_MIN = timedelta(minutes=int(config.env("NAIKEN_RESERVE_MIN", "15")))   # 今から15分後以降
RESERVE_MAX = timedelta(days=int(config.env("NAIKEN_RESERVE_DAYS", "7")))      # 7日先まで
RESERVE_HOURS = (8, 20)                       # 8:00〜20:00（夜間・早朝は相手先のご迷惑になるため）
RESERVE_LOCK = timedelta(minutes=5)           # 取り消し・変更は送信の5分前まで
RESERVE_BEFORE_VISIT = timedelta(hours=1)     # 内見の第1希望の1時間前まで
RESERVE_COUNT = int(config.env("NAIKEN_RESERVE_COUNT", "10"))  # 1人が同時に予約できる件数

RESERVE_ERRORS = {
    "bad_time": "送信の日時を読み取れませんでした。もう一度お選びください。",
    "too_soon": "送信の日時は、今から15分後以降でお選びください。すぐに送る場合は「この内容で送信」を押してください。",
    "too_far": "送信の日時は、7日先までの間でお選びください。",
    "night": "送信の日時は、8時〜20時の間でお選びください（夜間・早朝は相手先のご迷惑になるため、お送りしていません）。",
    "after_visit": "送信の日時は、内見の第1希望（{visit}）の1時間前までの間でお選びください。",
    "too_many": "送信予約は、同時に10件までです。予約中のFAXが送られてから、またお選びください。",
    "too_late": "送信の5分前を過ぎたため、取り消し・変更ができませんでした。このままお送りします。",
    "not_reserved": "この申込書は、予約中ではありません（送信済み・取り消し済みの可能性があります）。",
}


def parse_when(s):
    """「2026-11-01T10:00」（LINEの日時選択・アプリ）→ 日本時間の datetime。読めなければ None。"""
    try:
        t = datetime.fromisoformat(str(s).strip().replace(" ", "T"))
    except (TypeError, ValueError):
        return None
    return t.replace(tzinfo=store.JST) if t.tzinfo is None else t.astimezone(store.JST)


def when_label(t):
    t = t.astimezone(store.JST)
    return f"{t.month}月{t.day}日（{WEEK[t.weekday()]}）{t.hour}時{t.minute:02d}分"


def first_visit(d):
    """内見の希望日時のうち一番早いもの（日本時間）。時刻がなければ午前・未指定は9時、午後は12時とみなす。"""
    out = []
    for w in d.get("wishes") or []:
        try:
            day = datetime.strptime(w["date"], "%Y-%m-%d")
        except (KeyError, ValueError, TypeError):
            continue
        if w.get("time"):
            h, m = (int(x) for x in w["time"].split(":")[:2])
        else:
            h, m = (12 if "午後" in (w.get("note") or "") else 9), 0
        out.append(day.replace(hour=h, minute=m, tzinfo=store.JST))
    return min(out) if out else None


def reserve_error(d, when):
    """選ばれた日時が予約の決まりに合わなければ、その理由のコード（合えば None）。"""
    if not when:
        return "bad_time"
    now = store.now()
    if when < now + RESERVE_MIN:
        return "too_soon"
    if when > now + RESERVE_MAX:
        return "too_far"
    t = when.astimezone(store.JST)
    if not (RESERVE_HOURS[0] <= t.hour < RESERVE_HOURS[1] or (t.hour == RESERVE_HOURS[1] and t.minute == 0)):
        return "night"
    v = first_visit(d)
    if v and when > v - RESERVE_BEFORE_VISIT:
        return "after_visit"
    return None


def error_text(code, d=None):
    v = first_visit(d or {})
    return RESERVE_ERRORS.get(code, "").format(visit=wish_label({"date": v.strftime("%Y-%m-%d"), "time": v.strftime("%H:%M")}) if v else "")


def holiday_note(when):
    """不動産会社の定休日が多い水曜日を選んだときの一言。"""
    if when.astimezone(store.JST).weekday() == 2:
        return "水曜日は定休日の会社が多いため、ご確認が翌日になることがあります。"
    return ""


def reserved(uid):
    """予約中のFAX（送信の早い順）。[(fax_id, rec), …]"""
    rows = [(k, v) for k, v in store.db().find_all("faxes", "user_id", uid) if v.get("status") == "scheduled"]
    return sorted(rows, key=lambda x: x[1]["send_at"])


def reserve(uid, fid, when):
    """［日時を指定して送る］。今すぐ送るときと同じ確認をしてから、回数を押さえて預かる。戻り値は status。"""
    rec = store.db().get("faxes", fid)
    if not rec or rec["user_id"] != uid or rec["status"] not in ("preview", "failed"):
        return rec and rec["status"]
    u = store.get_user(uid)
    if is_trial(u):
        return "trial"
    err = reserve_error(rec["draft"], when)
    if err:
        return err
    if len(reserved(uid)) >= RESERVE_COUNT:
        return "too_many"
    if not can_send(u):
        return "limit"
    to = rec["draft"]["to"]
    if not fax_ok(to.get("fax")):
        return "fax_invalid"
    if opted_out(to.get("fax")):
        store.db().set("faxes", fid, {"status": "optout"}, merge=True)
        return "optout"
    count(uid, 1)
    source = (store.get_user(uid) or {}).get("last_fax_from")
    # 申込書の画像・PDFは送信日から7日間見られるようにする
    store.db().set("faxes", fid, {"status": "scheduled", "send_at": when.astimezone(store.JST), "from": source,
                                  "reserved_at": store.now(), "expires_at": when + timedelta(days=7)}, merge=True)
    return "scheduled"


def reschedule(uid, fid, when):
    """予約中のFAXの日時を変える（回数はそのまま）。戻り値は status またはエラーのコード。"""
    rec = store.db().get("faxes", fid)
    if not rec or rec["user_id"] != uid:
        return None
    if rec["status"] != "scheduled":
        return "not_reserved"
    if rec["send_at"] <= store.now() + RESERVE_LOCK:
        return "too_late"
    err = reserve_error(rec["draft"], when)
    if err:
        return err
    store.db().set("faxes", fid, {"send_at": when.astimezone(store.JST), "expires_at": when + timedelta(days=7)}, merge=True)
    return "scheduled"


def unreserve(uid, fid):
    """予約を取り消す。押さえた回数を戻し、申込書は確認中（preview）に戻す（そのまま今すぐ送ることもできる）。"""
    rec = store.db().get("faxes", fid)
    if not rec or rec["user_id"] != uid:
        return None
    if rec["status"] != "scheduled":
        return "not_reserved"
    if rec["send_at"] <= store.now() + RESERVE_LOCK:
        return "too_late"

    def fn(r):
        if r.get("status") != "scheduled":
            return False
        r.update(status="preview", send_at=None, canceled_at=store.now())
        return True
    if not store.db().update_tx("faxes", fid, fn):
        return "not_reserved"
    count(uid, -1, source=rec.get("from"))
    return "canceled"


def run_reserved():
    """定期実行（5分ごと）から。送信時刻になった予約を送る。送った件数を返す。"""
    import bot
    n = 0
    for fid, rec in store.db().before("faxes", "send_at", store.now()):
        if rec.get("status") != "scheduled":
            continue

        def claim(r):
            # 定期実行が重なっても二重に送らない
            if r.get("status") != "scheduled":
                return False
            r["status"] = "dispatching"
            return True
        if not store.db().update_tx("faxes", fid, claim):
            continue
        uid, source = rec["user_id"], rec.get("from")
        u = store.get_user(uid) or {}
        to = rec["draft"].get("to") or {}
        base = {"管理会社": " ".join(x for x in (to.get("company"), to.get("branch")) if x), "FAX番号": to.get("fax", "")}
        try:
            if opted_out(to.get("fax")):
                # 予約の後に元付業者さんから「FAXを止めて」と言われた番号
                store.db().set("faxes", fid, {"status": "optout"}, merge=True)
                count(uid, -1, source=source)
                _push(u, "予約していた内見申込書のFAXは、送り先の管理会社さまからFAXを止めるご依頼をいただいているため、"
                         f"お送りしませんでした（{base['管理会社']}）。お電話など直接ご連絡をお願いいたします。今回の分は、回数に数えていません。", sale=rec["draft"].get("kind") == "sale")
                continue
            status = _dispatch(uid, fid, rec, u, source)
        except Exception:
            log.exception("予約FAXを送れない %s", fid)
            store.db().set("faxes", fid, {"status": "failed"}, merge=True)
            count(uid, -1, source=source)
            status = "failed"
        if status == "sending":
            n += 1  # 届いた・失敗は delivery.poll_faxes → on_fax_result でお知らせする
        elif status == "failed":
            store.update_user(uid, {"nk_fid": fid, "nk": rec["draft"]})
            bot.notify(uid, "naiken_failed", {**base, "理由": "予約の時刻にFAXを送り出せませんでした",
                                               **({"_sale": True} if rec["draft"].get("kind") == "sale" else {})})
        elif status == "not_ready":
            _push(u, "予約していた内見申込書のFAXは、ただいまFAX送信の準備中のため、お送りしていません（テスト版）。",
                  sale=rec["draft"].get("kind") == "sale")
    return n


def _push(u, text, sale=False):
    import line
    if u.get("line_uid"):
        line.push(u["line_uid"], [line.text(sale_words(text) if sale else text)])


FAIL_REASONS = {"busy": "相手のFAXが話し中、または応答がありませんでした", "invalid": "FAX番号がつながりませんでした"}


def on_fax_result(fid, st, reason=""):
    """faximo の送信結果（delivery.poll_faxes から）。届いた・再送中・失敗を利用者に知らせる。"""
    import bot
    rec = store.db().get("faxes", fid)
    if not rec:
        return
    uid, d = rec["user_id"], rec["draft"]
    to = d.get("to") or {}
    base = {"管理会社": " ".join(x for x in (to.get("company"), to.get("branch")) if x), "FAX番号": to.get("fax", "")}
    if d.get("kind") == "sale":
        base["_sale"] = True  # 知らせる文面を売買の言い方に（bot.notify で置き換える）
    if st == "sent":
        store.db().set("faxes", fid, {"status": "sent", "done_at": store.now()}, merge=True)
        u = store.get_user(uid) or {}
        month, packs, _ = remaining(u)
        live = sorted([p for p in (u.get("fax_packs") or []) if p["exp"] > store.now() and p["used"] < p["n"]],
                      key=lambda p: p["exp"])
        p = d.get("prop") or {}
        bot.notify(uid, "naiken_sent", {**base, "物件名": p.get("name", ""), "号室": p.get("room", ""),
                                        "自社FAX": (u.get("company") or {}).get("FAX", ""),
                                        "リンク": file_url(fid, rec, "naiken.pdf"), "内見残": month,
                                        "追加FAXの案内": f"＋追加分{packs}回（{bot._md(live[0]["exp"])}まで）" if packs else ""})
    elif st == "retrying":
        if not rec.get("retry_told"):
            store.db().set("faxes", fid, {"retry_told": True}, merge=True)
            bot.notify(uid, "naiken_retrying", {"_sale": True} if d.get("kind") == "sale" else {})
    elif st == "failed":
        store.db().set("faxes", fid, {"status": "failed", "done_at": store.now()}, merge=True)
        count(uid, -1, source=rec.get("from"))  # 届かなかった分は回数に数えない
        store.update_user(uid, {"nk_fid": fid, "nk": d})  # 「もう一度送る」「番号を直して送る」で使う
        bot.notify(uid, "naiken_failed", {**base, "理由": FAIL_REASONS.get(reason, reason or "相手のFAXにつながりませんでした")})


# ---------- 送信停止リスト（元付業者さんからの「FAXを止めて」） ----------

def _optout_id(number):
    return re.sub(r"\D", "", normalize_fax(number) or "")


def opted_out(number):
    n = _optout_id(number)
    return bool(n) and bool(store.db().get("fax_optout", n))


def optout_add(number, note=""):
    n = _optout_id(number)
    if not n:
        return None
    store.db().set("fax_optout", n, {"number": fax_display(number), "note": note, "at": store.now()})
    return n


def optout_remove(number):
    n = _optout_id(number)
    if n and store.db().get("fax_optout", n):
        store.db().delete("fax_optout", n)
        return True
    return False


def optout_list():
    return [{"id": k, **{x: (v[x].isoformat() if x == "at" else v[x]) for x in ("number", "note", "at") if v.get(x)}}
            for k, v in store.db().all("fax_optout")]


def remember_to(uid, to):
    def fn(u):
        h = [x for x in (u.get("naiken_history") or []) if normalize_fax(x.get("fax")) != normalize_fax(to.get("fax"))]
        u["naiken_history"] = ([{"company": to.get("company", ""), "branch": to.get("branch", ""),
                                 "fax": to.get("fax", "")}] + h)[:10]
    store.db().update_tx("users", uid, fn)
