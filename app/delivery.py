"""メール・FAXでのお届け。

メールは Resend（RESEND_API_KEY があれば送る）。FAXは送信サービスが決まってから足す。
"""
import base64
import logging

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
    return r.status_code < 300


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
    return r.status_code < 300


def send_fax(number, pdf: bytes) -> bool:
    log.info("FAX未設定のため送らない to=%s", number)
    return False
