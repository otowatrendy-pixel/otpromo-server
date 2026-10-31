"""LINE の push とメールの通数を数えて見張る（10/28本人決定）。

・数え方：日本時間の日ごと・月ごとに meta/usage_d_YYYYMMDD・meta/usage_m_YYYYMM に足していく。
・警告（その日のうちに1回ずつ、件名の先頭に【至急】）
    LINE push … 上限の50%・75%を超えたとき、月末の見込みが上限を超えそうなとき
    メール   … 1日50通を超えた日、月2,000通を超えたとき、送れなかったとき（LINEで代わりに届けた）
・push が上限で送れなかったお知らせは、その方の held_pushes にためて、次の応答（通数に数えない）でまとめて届ける。
・毎朝9時の日報【1分資料 日報】と、数字を返す /admin/usage（合言葉 ADMIN_USAGE_KEY）。
・運営への知らせはメール（ADMIN_MAIL）と、運営専用LINE（ADMIN_LINE_TOKEN があれば、友だちになった方へ push）。
"""
import calendar
import logging
from datetime import timedelta

import requests

import config
import line
import store

log = logging.getLogger("usage")
ADMIN_MAIL = config.env("ADMIN_MAIL", "m-aij@otpromo.com")
ADMIN_LINE_TOKEN = config.env("ADMIN_LINE_TOKEN")
ADMIN_LINE_SECRET = config.env("ADMIN_LINE_SECRET")
LINE_PUSH_LIMIT = int(config.env("LINE_PUSH_LIMIT", "200") or 200)  # 無料プラン200通。ライトなら5000
MAIL_DAY_LIMIT = int(config.env("MAIL_DAY_LIMIT", "100") or 100)     # Resend 無料枠
MAIL_MONTH_LIMIT = int(config.env("MAIL_MONTH_LIMIT", "3000") or 3000)
MAIL_DAY_WARN = int(config.env("MAIL_DAY_WARN", "50") or 50)
MAIL_MONTH_WARN = int(config.env("MAIL_MONTH_WARN", "2000") or 2000)
HELD_MAX = 20
# 費用の上限（10/29本人承認）：AI利用料が月3万円で警告、月5万円で新しい資料づくりを止める。1人1日60件まで
AI_WARN_YEN = int(config.env("AI_WARN_YEN", "30000") or 30000)
AI_STOP_YEN = int(config.env("AI_STOP_YEN", "50000") or 50000)
DAILY_DOC_LIMIT = int(config.env("DAILY_DOC_LIMIT", "60") or 60)


def _jst(t=None):
    return (t or store.now()).astimezone(store.JST)


def _day(t=None):
    return _jst(t).strftime("%Y%m%d")


def _month(t=None):
    return _jst(t).strftime("%Y%m")


def count(kind, n=1):
    """kind: push・mail・push_held（上限でためた）・mail_fallback（メールの代わりにLINEで届けた）。"""
    try:
        store.db().incr("meta", "usage_d_" + _day(), {kind: n})
        store.db().incr("meta", "usage_m_" + _month(), {kind: n})
    except Exception:
        log.exception("通数を数えられませんでした %s", kind)


# 売買版：1件あたりのAI原価（10/31本人OK）。料金の試算は1件15円、使い放題は1件約34円を超えると赤字
DOC_COST_WARN_YEN = float(config.env("DOC_COST_WARN_YEN", "20"))


def note_doc_cost(yen):
    """読み取り1件ぶんのAI原価（円）を、日ごと・月ごとの件数・合計・最大に足す。失敗しても資料づくりは止めない。"""
    try:
        yen = float(yen)
        for doc in ("usage_d_" + _day(), "usage_m_" + _month()):
            store.db().incr("meta", doc, {"doc_ai_n": 1, "doc_ai_yen": round(yen, 2)})
            if yen > _get(doc).get("doc_ai_max", 0):
                store.db().set("meta", doc, {"doc_ai_max": round(yen, 2)}, merge=True)
    except Exception:
        log.exception("1件あたりのAI原価を数えられませんでした")


def doc_cost(d):
    """{"n", "avg", "max"}（件数が0なら avg・max は None）。"""
    n = d.get("doc_ai_n", 0)
    return {"n": n, "avg": round(d.get("doc_ai_yen", 0) / n, 1) if n else None,
            "max": round(d.get("doc_ai_max", 0), 1) if n else None}


def _doc_cost_lines():
    """日報の行（売買版だけ）。昨日の平均か最大が DOC_COST_WARN_YEN を超えたら、別に【至急】で知らせる。"""
    y = doc_cost(_get("usage_d_" + _day(store.now() - timedelta(days=1))))
    m = doc_cost(_get("usage_m_" + _month()))
    fmt = lambda c: f"平均{c['avg']:.1f}円・最大{c['max']:.1f}円（{c['n']}件）" if c["n"] else "読み取りなし"
    text = f"■1件あたりのAI原価（目安{DOC_COST_WARN_YEN:g}円まで）\n昨日：{fmt(y)}\n今月：{fmt(m)}\n"
    over = [f"{k}{v:.1f}円" for k, v in (("平均", y["avg"]), ("最大", y["max"])) if v is not None and v > DOC_COST_WARN_YEN]
    if over:
        alert_once("doc_cost", f"1件あたりのAI原価が{DOC_COST_WARN_YEN:g}円を超えました",
                   f"昨日の読み取り{y['n']}件のうち、{'・'.join(over)}でした（目安{DOC_COST_WARN_YEN:g}円）。\n"
                   f"料金の試算は1件15円です。使い放題プランは、1件約34円を超えると赤字になります。\n今月：{fmt(m)}")
    return text


def _get(doc):
    return store.db().get("meta", doc) or {}


# ---------- 運営への知らせ ----------

def notify_admin(subject, text, urgent=True):
    import delivery
    subject = ("【至急】" if urgent else "") + subject
    delivery.send_notice(ADMIN_MAIL, subject, text)
    admin_line(f"{subject}\n\n{text}")


def admin_line(text):
    """運営専用LINE（「1分資料 運営通知」）へ。トークンと友だちの登録があるときだけ。"""
    if not ADMIN_LINE_TOKEN:
        return
    for uid in (_get("admin_line").get("user_ids") or []):
        try:
            requests.post(f"{line.API}/message/push", timeout=20,
                          headers={"Authorization": f"Bearer {ADMIN_LINE_TOKEN}"},
                          json={"to": uid, "messages": [{"type": "text", "text": text[:5000]}]})
        except requests.RequestException:
            log.warning("運営LINEに送れませんでした")


def admin_line_webhook(body: bytes, signature: str, events: list):
    """運営専用LINEの友だち追加などで、運営の方のユーザーIDを覚える。"""
    import base64
    import hashlib
    import hmac
    if not ADMIN_LINE_SECRET:
        return False
    mac = hmac.new(ADMIN_LINE_SECRET.encode(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(base64.b64encode(mac).decode(), signature or ""):
        return False
    for ev in events:
        uid = (ev.get("source") or {}).get("userId")
        if not uid or ev.get("type") not in ("follow", "message"):
            continue
        ids = _get("admin_line").get("user_ids") or []
        if uid not in ids:
            store.db().set("meta", "admin_line", {"user_ids": ids + [uid]}, merge=True)
        if ev.get("replyToken"):
            requests.post(f"{line.API}/message/reply", timeout=20,
                          headers={"Authorization": f"Bearer {ADMIN_LINE_TOKEN}"},
                          json={"replyToken": ev["replyToken"], "messages": [{"type": "text", "text":
                                "1分資料の運営通知を、このLINEにお送りします。"}]})
    return True


def alert_once(key, subject, text):
    """同じ警告は、その日のうちに1回だけ。"""
    if not store.db().create("meta", f"alert_{_day()}_{key}", {"at": store.now()}):
        return False
    notify_admin(subject, text)
    return True


# ---------- LINE push ----------

def on_push(to, messages, status, body):
    """line.push の結果ごとに呼ばれる。"""
    if status == 200:
        count("push")
        return
    # 月の上限に達すると 429（You have reached your monthly limit.）
    if status == 429 and "monthly limit" in (body or "").lower():
        hold(to, messages)


def hold(line_uid, messages):
    uid, u = store.user_by_line(line_uid, create=False)
    if not uid:
        return
    held = (u.get("held_pushes") or []) + [{"at": store.now(), "messages": messages}]
    store.update_user(uid, {"held_pushes": held[-HELD_MAX:], "held_pushes_at": u.get("held_pushes_at") or store.now()})
    count("push_held", len(messages))
    alert_once("push_limit", "LINEの通数が上限に達しました",
               "LINEの push が月の上限に達したため、お知らせを送れませんでした。\n"
               "送れなかったお知らせはためておき、その方から次にメッセージが届いたときに返信と一緒にお届けします。\n"
               "急ぐ場合は、LINE公式アカウントのプランの変更をご検討ください。")


def with_held(u, messages):
    """応答（通数に数えない）に、ためていたお知らせを足す。戻り値は (送るメッセージ, 残すお知らせ)。"""
    held = list(u.get("held_pushes") or [])
    out = list(messages)
    rest = []
    for h in held:
        ms = h.get("messages") or []
        if len(out) + len(ms) <= 5:
            out = out + ms
        else:
            rest.append(h)
    return out, rest


def line_quota():
    """LINEの今月の push 数と上限（API から。取れなければ自分で数えた数と環境変数の上限）。"""
    used = limit = None
    try:
        h = line._h()
        r = requests.get(f"{line.API}/message/quota/consumption", headers=h, timeout=10)
        if r.status_code == 200:
            used = r.json().get("totalUsage")
        r = requests.get(f"{line.API}/message/quota", headers=h, timeout=10)
        if r.status_code == 200 and r.json().get("type") == "limited":
            limit = r.json().get("value")
    except requests.RequestException:
        pass
    own = _get("usage_m_" + _month()).get("push", 0)
    return (used if used is not None else own), (limit or LINE_PUSH_LIMIT)


# ---------- 数字と警告 ----------

def stats():
    t = _jst()
    days = calendar.monthrange(t.year, t.month)[1]
    elapsed = t.day - 1 + (t.hour * 60 + t.minute) / 1440
    m, d = _get("usage_m_" + _month()), _get("usage_d_" + _day())
    y = _get("usage_d_" + _day(store.now() - timedelta(days=1)))
    push_used, push_limit = line_quota()
    forecast = round(push_used / elapsed * days) if elapsed >= 0.5 else push_used
    return {
        "as_of": t.isoformat(timespec="minutes"),
        "line_push": {"month": push_used, "limit": push_limit, "percent": round(push_used * 100 / push_limit, 1),
                      "today": d.get("push", 0), "yesterday": y.get("push", 0), "forecast_month_end": forecast,
                      "held_now": _held_now(), "held_month": m.get("push_held", 0)},
        "users": user_stats(),
        "mail": {"today": d.get("mail", 0), "yesterday": y.get("mail", 0), "day_limit": MAIL_DAY_LIMIT,
                 "month": m.get("mail", 0), "month_limit": MAIL_MONTH_LIMIT,
                 "fallback_to_line_month": m.get("mail_fallback", 0), "fallback_to_line_yesterday": y.get("mail_fallback", 0)},
    }


def user_stats():
    """利用者数（日報用）。有料はプランと払い方の内訳、お試し中、今月の新規登録、今月の解約。"""
    t = _jst()
    first = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    paid = {"light_month": 0, "light_year": 0, "unlimited_month": 0, "unlimited_year": 0}
    trial = new = ended = blocked = 0
    for _, u in store.db().all("users"):
        if u.get("review"):
            continue  # ストア審査用のアカウントは数えない
        plan = u.get("plan") or "free"
        if plan in ("light", "unlimited"):
            key = f"{plan}_{'year' if u.get('billing_interval') == 'year' else 'month'}"
            paid[key] += 1
        elif u.get("registered") and not u.get("blocked"):
            trial += 1
        if u.get("registered_at") and u["registered_at"] >= first:
            new += 1
        if u.get("ended_at") and u["ended_at"] >= first:
            ended += 1
        if u.get("blocked"):
            blocked += 1
    return {"paid": paid, "paid_total": sum(paid.values()), "trial": trial, "new_this_month": new,
            "ended_this_month": ended, "blocked": blocked}


def _held_now():
    try:
        return sum(len(u.get("held_pushes") or []) for _, u in store.db().before("users", "held_pushes_at", store.now()))
    except Exception:
        return None


def check():
    """定期実行（5分ごと）から。しきい値を超えたら、その日のうちに1回ずつ知らせる。"""
    s = stats()
    p, ml = s["line_push"], s["mail"]
    sent = []
    head = f"LINE push：今月{p['month']}通／上限{p['limit']}通（{p['percent']}%）、月末の見込み{p['forecast_month_end']}通"
    for pct in (75, 50):
        if p["percent"] >= pct:
            if alert_once(f"push{pct}", f"LINEの通数が上限の{pct}%を超えました", head):
                sent.append(f"push{pct}")
            break
    if p["forecast_month_end"] > p["limit"]:
        if alert_once("push_forecast", "LINEの通数が月末に上限を超えそうです", head):
            sent.append("push_forecast")
    yen = _get("usage_m_" + _month()).get("ai_yen", 0)
    if yen >= AI_WARN_YEN:
        if alert_once("ai_warn_" + _month(), f"AI利用料が月{AI_WARN_YEN:,}円を超えました",
                      f"今月のAI利用料：約{round(yen):,}円\n月{AI_STOP_YEN:,}円に達すると、新しい資料づくりを自動で止めます。"):
            sent.append("ai_warn")
    if ml["today"] > MAIL_DAY_WARN:
        if alert_once("mail_day", f"メールの送信数が1日{MAIL_DAY_WARN}通を超えました",
                      f"今日のメール：{ml['today']}通／1日{MAIL_DAY_LIMIT}通（Resend無料枠）\n今月：{ml['month']}通／{MAIL_MONTH_LIMIT}通"):
            sent.append("mail_day")
    if ml["month"] > MAIL_MONTH_WARN:
        if alert_once("mail_month", f"メールの送信数が月{MAIL_MONTH_WARN}通を超えました",
                      f"今月のメール：{ml['month']}通／{MAIL_MONTH_LIMIT}通（Resend無料枠）"):
            sent.append("mail_month")
    return sent


def _growth_lines():
    try:
        import growth
        return growth.daily_lines()
    except Exception:
        log.exception("日報の追加の行")
        return "（読み取り・解約理由などの集計に失敗しました）"


def daily_report():
    """毎朝9時の日報【1分資料 日報】。"""
    s = stats()
    p, ml, us = s["line_push"], s["mail"], s["users"]
    text = (f"{_jst().month}月{_jst().day}日 {_jst().hour}時時点の数字です。\n\n"
            f"■LINE push\n今月：{p['month']}通／上限{p['limit']}通（{p['percent']}%）\n"
            f"昨日：{p['yesterday']}通\n月末の見込み：{p['forecast_month_end']}通\n\n"
            f"■メール\n昨日：{ml['yesterday']}通／1日{ml['day_limit']}通\n今月：{ml['month']}通／{ml['month_limit']}通\n\n"
            f"■利用者\n有料：{us['paid_total']}人（ライト 月払い{us['paid']['light_month']}・年払い{us['paid']['light_year']}／"
            f"使い放題 月払い{us['paid']['unlimited_month']}・年払い{us['paid']['unlimited_year']}）\n"
            f"お試し中：{us['trial']}人\n今月の新規登録：{us['new_this_month']}人\n今月の解約：{us['ended_this_month']}人\n\n"
            f"{_growth_lines()}\n\n"
            + (_doc_cost_lines() + "\n" if config.APP_MODE == "sale" else "") +
            f"■上限で送れずにためているLINEのお知らせ：{p['held_now'] if p['held_now'] is not None else '－'}件\n"
            f"■メールが送れずLINEで代わりに届けた件数：昨日{ml['fallback_to_line_yesterday']}件（今月{ml['fallback_to_line_month']}件）\n")
    import delivery
    delivery.send_notice(ADMIN_MAIL, "【1分資料 売買版 日報】" if config.APP_MODE == "sale" else "【1分資料 日報】", text)
    return s


# ---------- 障害・メンテナンスのお知らせ（運営から全員へ） ----------

_outage = {"at": 0, "v": None}


def outage():
    """「障害中」スイッチ（meta/outage）。30秒だけ覚えておき、イベントごとに読みに行かない。"""
    import time
    if time.monotonic() - _outage["at"] > 30:
        _outage["v"] = _get("outage")
        _outage["at"] = time.monotonic()
    v = _outage["v"] or {}
    return v if v.get("on") else None


def set_outage(on, values=None):
    store.db().set("meta", "outage", {"on": bool(on), "values": values or {}, "at": store.now()})
    _outage["at"] = 0


def _recipients(to):
    out = []
    for uid, u in store.db().all("users"):
        if u.get("review"):
            continue
        if to == "line":
            if u.get("line_uid") and not u.get("blocked"):
                out.append((uid, u))
        elif u.get("registered") and ((u.get("delivery") or {}).get("email") or (u.get("company") or {}).get("担当者メール")):
            out.append((uid, u))
    return out


def broadcast_plan(mid, to, values):
    """送る前の確認：人数・残りの通数で足りるか・文面の見本。"""
    import texts
    people = _recipients(to)
    sample = texts.raw(mid, **{"会社名": "（会社名）", "担当者名": "（担当者名）", **(values or {})})
    if to == "line":
        used, limit = line_quota()
        left = max(0, limit - used)
        return {"to": "line", "people": len(people), "push_left": left, "enough": len(people) <= left, "preview": sample}
    s = stats()["mail"]
    left = max(0, MAIL_DAY_LIMIT - s["today"])
    return {"to": "mail", "people": len(people), "mail_left_today": left, "enough": len(people) <= left,
            "preview": sample}


def broadcast(mid, to, values):
    """一斉送信（呼び出し側で別スレッドにする）。送れた人数を返す。"""
    import delivery
    import texts
    n = 0
    for uid, u in _recipients(to):
        co = u.get("company") or {}
        kw = {"会社名": co.get("会社名", ""), "担当者名": co.get("担当者名", ""), **(values or {})}
        try:
            if to == "line":
                ok = line.push(u["line_uid"], [texts.build(mid, **kw)])
            else:
                body = texts.raw(mid, **kw)
                subject, _, rest = body.partition("\n")
                subject = subject.removeprefix("件名：").strip()
                addr = (u.get("delivery") or {}).get("email") or co.get("担当者メール")
                ok = delivery.send_notice(addr, subject, rest.strip())
            n += 1 if ok else 0
        except Exception:
            log.exception("一斉送信の失敗 %s", uid)
    notify_admin(f"一斉送信が終わりました（{mid}）", f"宛先：{'LINE' if to == 'line' else 'メール'}\n送れた人数：{n}人", urgent=False)
    return n


# ---------- 費用の上限 ----------

_ai = {"at": 0, "yen": 0}


def ai_month_yen():
    import time
    if time.monotonic() - _ai["at"] > 60:
        _ai["yen"] = _get("usage_m_" + _month()).get("ai_yen", 0)
        _ai["at"] = time.monotonic()
    return _ai["yen"]


def ai_stopped():
    """今月のAI利用料が上限（5万円）に達したら、新しい資料づくりを止める。"""
    yen = ai_month_yen()
    if yen >= AI_STOP_YEN:
        alert_once("ai_stop_" + _month(), f"AI利用料が月{AI_STOP_YEN:,}円に達したため、新しい資料づくりを止めました",
                   f"今月のAI利用料：約{round(yen):,}円\n利用者には「混み合っています」とお伝えしています。\n"
                   f"再開するには、Cloud Run の環境変数 AI_STOP_YEN を上げてください。")
        return True
    return False


def over_daily(u):
    """1人1日の作成上限（60件、翌日に戻る）に達しているか。"""
    today = _day()
    return u.get("day_key") == today and (u.get("day_docs") or 0) >= DAILY_DOC_LIMIT


def busy_message():
    import texts
    if "busy_cost_limit" in texts._MSG:
        return texts.build("busy_cost_limit")
    return line.text("申し訳ありません。ただいま大変混み合っているため、資料づくりを一時お休みしています。\n"
                     "今回の分は、件数に数えていません。準備ができしだい、このLINEでお知らせします。")


def daily_limit_message():
    import texts
    if "daily_limit_reached" in texts._MSG:
        return texts.build("daily_limit_reached", 上限=DAILY_DOC_LIMIT)
    return line.text(f"本日の資料づくりは、上限の{DAILY_DOC_LIMIT}件に達しました。\n"
                     "明日0時から、またお使いいただけます。今回の分は、件数に数えていません。")
