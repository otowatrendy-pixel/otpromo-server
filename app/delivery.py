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


def send_mail(to, title, pdf: bytes, pdf_url: str) -> bool:
    if not RESEND_API_KEY:
        log.info("メール未設定のため送らない to=%s", to)
        return False
    body = (f"いつも「1分資料」をご利用いただき、ありがとうございます。\n\n"
            f"物件「{title}」の資料をお送りします（PDFを添付しています）。\n"
            f"添付が開けない場合は、こちらからもご覧いただけます。\n{pdf_url}\n\n"
            f"AIが作った下書きです。お客様にお渡しする前に、内容をご確認ください。\n\n"
            f"――――――――――\n1分資料（otpromo.com）\nお問い合わせ：{MAIL_REPLY_TO}\n")
    r = requests.post("https://api.resend.com/emails", timeout=30,
                      headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                      json={"from": MAIL_FROM, "to": [to], "reply_to": MAIL_REPLY_TO,
                            "subject": f"【1分資料】{title}の資料", "text": body,
                            "attachments": [{"filename": "shiryou.pdf",
                                             "content": base64.b64encode(pdf).decode()}]})
    if r.status_code >= 300:
        log.warning("メール送信失敗 %s %s", r.status_code, r.text[:300])
    return r.status_code < 300


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
