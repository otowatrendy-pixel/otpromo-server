"""LINE Messaging API の呼び出し。

通数を抑えるため、返事はできるだけ「応答（reply）」で返す。応答の期限切れなどで失敗したときだけ push を使う。
"""
import base64
import hashlib
import hmac
import logging

import requests

import config

API = "https://api.line.me/v2/bot"
DATA = "https://api-data.line.me/v2/bot"
log = logging.getLogger("line")


def _h():
    return {"Authorization": f"Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}"}


def verify(body: bytes, signature: str) -> bool:
    if not config.LINE_CHANNEL_SECRET:
        return False
    mac = hmac.new(config.LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature or "")


def reply(token, messages) -> bool:
    r = requests.post(f"{API}/message/reply", headers=_h(), json={"replyToken": token, "messages": messages[:5]},
                      timeout=20)
    if r.status_code != 200:
        log.warning("reply失敗 %s %s", r.status_code, r.text[:300])
    return r.status_code == 200


def push(to, messages) -> bool:
    r = requests.post(f"{API}/message/push", headers=_h(), json={"to": to, "messages": messages[:5]}, timeout=20)
    if r.status_code != 200:
        log.warning("push失敗 %s %s", r.status_code, r.text[:300])
    return r.status_code == 200


def reply_or_push(token, to, messages):
    """応答で返し、だめなら push（月の通数に数えられる）。"""
    if token and reply(token, messages):
        return "reply"
    return "push" if push(to, messages) else "failed"


def loading(user_id, seconds=60):
    """「入力中…」の表示。通数には数えない。"""
    try:
        requests.post(f"{API}/chat/loading/start", headers=_h(),
                      json={"chatId": user_id, "loadingSeconds": seconds}, timeout=10)
    except requests.RequestException:
        pass


def content(message_id) -> bytes:
    r = requests.get(f"{DATA}/message/{message_id}/content", headers=_h(), timeout=60)
    r.raise_for_status()
    return r.content


def profile(user_id) -> dict:
    try:
        r = requests.get(f"{API}/profile/{user_id}", headers=_h(), timeout=10)
        return r.json() if r.status_code == 200 else {}
    except requests.RequestException:
        return {}


# ---------- メッセージの部品 ----------

def text(t, quick=None):
    m = {"type": "text", "text": t[:5000]}
    if quick:
        m["quickReply"] = {"items": quick}
    return m


def qr_message(label, t=None):
    return {"type": "action", "action": {"type": "message", "label": label[:20], "text": t or label}}


def qr_postback(label, data, display=None):
    return {"type": "action", "action": {"type": "postback", "label": label[:20], "data": data,
                                         "displayText": display or label}}


def image(original, preview):
    return {"type": "image", "originalContentUrl": original, "previewImageUrl": preview}


BLUE = "#1A56A8"


def button_card(alt, title, body, buttons, note=None):
    """見出し・本文・ボタンのカード（Flex Message）。buttons は (ラベル, URL) の並び。"""
    contents = [{"type": "text", "text": title, "weight": "bold", "size": "md", "wrap": True, "color": BLUE}]
    if body:
        contents.append({"type": "text", "text": body, "size": "sm", "wrap": True, "margin": "md", "color": "#333333"})
    if note:
        contents.append({"type": "text", "text": note, "size": "xxs", "wrap": True, "margin": "md", "color": "#888888"})
    footer = [{"type": "button", "style": "primary" if i == 0 else "secondary", "color": BLUE if i == 0 else None,
               "height": "sm", "action": {"type": "uri", "label": lab[:40], "uri": url}}
              for i, (lab, url) in enumerate(buttons)]
    for b in footer:
        if b["color"] is None:
            del b["color"]
    return {"type": "flex", "altText": alt[:400], "contents": {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "contents": contents},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer},
    }}
