"""メール・FAXでのお届け。

メールは Resend（RESEND_API_KEY があれば送る）。FAXは faximo（送信APIは仕様書が届いてからつなぐ、結果は問い合わせ方式）。
"""
import base64
import logging
from datetime import timedelta

import requests

import config

log = logging.getLogger("delivery")
MAIL_FROM = config.env("MAIL_FROM", "1分資料 <shiryou@otpromo.com>")
MAIL_REPLY_TO = config.env("MAIL_REPLY_TO", "m-aij@otpromo.com")
RESEND_API_KEY = config.env("RESEND_API_KEY")
FAX_ENABLED = bool(config.env("FAX_API_KEY"))


def mail_ready():
    return bool(RESEND_API_KEY)


def fax_ready():
    return FAX_ENABLED


def send_mail(to, title, files, pdf_url: str) -> bool:
    """資料をメールで送る。files は [(ファイル名, 中身), …]（お客様向け・社内向けの2つ）。"""
    if not RESEND_API_KEY:
        log.info("メール未設定のため送らない to=%s", to)
        return False
    if isinstance(files, (bytes, bytearray)):
        files = [(f"{title}.pdf", files)]
    split = len(files) > 1
    body = (f"いつも「1分資料」をご利用いただき、ありがとうございます。\n\n"
            f"物件「{title}」の資料をお送りします（PDFを添付しています）。\n"
            + ("・お客様向け：帯替え図面・初期費用の概算書（そのままお客様に送れます）\n"
               "・社内向け：おすすめポイント・想定Q&A・内見申込書（接客の準備用です。お客様には送らないでください）\n"
               if split else "")
            + f"添付が開けない場合は、こちらからもご覧いただけます。\n{pdf_url}\n\n"
            f"AIが作った下書きです。お客様にお渡しする前に、内容をご確認ください。\n\n"
            f"――――――――――\n1分資料（otpromo.com）\nお問い合わせ：{MAIL_REPLY_TO}\n")
    r = requests.post("https://api.resend.com/emails", timeout=30,
                      headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                      json={"from": MAIL_FROM, "to": [to], "reply_to": MAIL_REPLY_TO,
                            "subject": f"【1分資料】{title}の資料", "text": body,
                            "attachments": [{"filename": _safe_name(n), "content": base64.b64encode(b).decode()}
                                            for n, b in files]})
    if r.status_code >= 300:
        log.warning("メール送信失敗 %s %s", r.status_code, r.text[:300])
    else:
        _count()
    return r.status_code < 300


def _count():
    import usage
    usage.count("mail")


def _safe_name(name):
    """添付ファイル名に使えない文字を除く。"""
    return "".join("_" if ch in '\\/:*?"<>|' else ch for ch in name)[:80]


def send_notice(to, subject, text) -> bool:
    """運営あてのお知らせメール（確認リストなど）。"""
    if not RESEND_API_KEY:
        log.info("メール未設定のため送らない（お知らせ） %s", subject)
        return False
    r = requests.post("https://api.resend.com/emails", timeout=30,
                      headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                      json={"from": MAIL_FROM, "to": [to], "subject": subject, "text": text})
    if r.status_code >= 300:
        log.warning("お知らせメール失敗 %s %s", r.status_code, r.text[:300])
    else:
        _count()
    return r.status_code < 300


FAX_CHECK_MIN = 2      # 送信結果を問い合わせる間隔（分）
FAX_GIVEUP_HOURS = 12  # これを過ぎても結果が分からなければ運営に知らせる


def send_fax(number, pdf: bytes, sender: str = "", kind: str = "", ref: str = "", uid: str = "", meta=None):
    """FAXを送る。sender は相手のFAXの上に出る送信元の表示（「利用会社名（1分資料経由）」）。
    戻り値は送信の受付番号（受け付けられなければ None）。faximo には Webhook がないため（10/29回答）、
    結果（届いた・話し中で再送中・失敗）は poll_faxes が定期実行から問い合わせる。
    kind は結果を返す先（"naiken"＝内見申込書、"recv"＝受け取り用FAX）、ref はその資料のID。"""
    import store
    import usage
    u = store.get_user(uid) if uid else None
    if u and u.get("review"):
        # ストア審査用のアカウントは実際には送らない（知らない相手に届かないように）。結果は「届いた」にする
        import secrets
        pid = "REVIEW-" + secrets.token_hex(6)
        log.info("審査用アカウントのためFAXは送らない to=%s", number)
    else:
        pid = _fax_api_send(number, pdf, sender)
    if not pid:
        usage.count("fax_fail")
        return None
    store.db().set("fax_sends", pid, {"kind": kind, "ref": ref, "uid": uid, "number": number, "meta": meta or {},
                                      "status": "sending", "at": store.now(),
                                      "next_check": store.now() + timedelta(minutes=FAX_CHECK_MIN)})
    return pid


def _fax_api_send(number, pdf, sender):
    """faximo の送信API。仕様書（トライアル開始時に入手）が届いたらここをつなぐ。戻り値は受付番号。"""
    log.info("FAX未設定のため送らない to=%s sender=%s", number, sender)
    return None


def _fax_api_status(pid):
    """faximo の送信結果API。戻り値は ("sending"|"retrying"|"sent"|"failed", 理由)。仕様書が届いたらつなぐ。"""
    return "sending", ""


def poll_faxes():
    """定期実行（5分ごと）から。送信中のFAXの結果を問い合わせ、決まったものを元の機能に返す。件数を返す。"""
    import store
    import usage
    now = store.now()
    n = 0
    for pid, r in store.db().before("fax_sends", "next_check", now):
        if r.get("status") not in ("sending", "retrying"):
            continue
        try:
            st, reason = ("sent", "") if pid.startswith("REVIEW-") else _fax_api_status(pid)
        except Exception:
            log.exception("FAXの結果を取れない %s", pid)
            st, reason = r["status"], ""
        upd = {"next_check": now + timedelta(minutes=FAX_CHECK_MIN)}
        if st in ("sent", "failed"):
            upd = {"status": st, "reason": reason, "done_at": now, "next_check": None}
            if st == "failed":
                usage.count("fax_fail")
        elif r["at"] <= now - timedelta(hours=FAX_GIVEUP_HOURS):
            upd = {"status": "unknown", "next_check": None}
            usage.notify_admin("FAXの送信結果が分かりません",
                               f"受付番号：{pid}\n送り先：{r.get('number')}\n種類：{r.get('kind')}\n"
                               f"送信から{FAX_GIVEUP_HOURS}時間たっても結果が取れません。faximo の管理画面で確認してください。")
        elif st == "retrying" and r["status"] != "retrying":
            upd["status"] = "retrying"
        else:
            st = None
        store.db().set("fax_sends", pid, upd, merge=True)
        if st:
            _fax_result(r, st, reason)
            n += 1
    return n


def _fax_result(r, st, reason):
    try:
        if r.get("kind") == "naiken":
            import naiken
            naiken.on_fax_result(r["ref"], st, reason)
        elif r.get("kind") == "recv":
            import faxopt
            faxopt.on_fax_result(r, st, reason)
    except Exception:
        log.exception("FAXの結果の処理に失敗 %s", r)


def fax_sender(company_name):
    """FAXの送信元の表示：「利用会社名（1分資料経由）」（10/29本人承認）。"""
    name = (company_name or "").strip()
    return f"{name}（1分資料経由）" if name else "1分資料"
