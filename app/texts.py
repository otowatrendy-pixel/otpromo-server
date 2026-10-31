"""LINEの文面。messages.json（文面担当が作った一式）を読み、LINEのメッセージに組み立てる。

ボタンの書き方（messages.json の action）
  postback k=v   … ボタンを押すとサーバーに k=v が届く
  camera / cameraRoll … カメラ・写真を開く
  uri <URL>      … ページを開く（Stripeの支払いページなどは呼び出し側で URL を渡す）
URIのボタンがあるときはカード（Flex）、ないときは文＋クイックリプライにする。
"""
import json
import re
from pathlib import Path

import line

_PATH = Path(__file__).parent / "messages.json"
_MSG = {}
COMMON = {}


def load():
    global _MSG, COMMON
    d = json.loads(_PATH.read_text(encoding="utf-8"))
    COMMON = d.get("common", {})
    _MSG = {m["id"]: m for s in d["sections"] for m in s["messages"]}
    # 売買版の文面（ID は sale_ で始まり、賃貸版と重ならない）。SALE_ENABLED=0 のあいだは使われない
    sale = _PATH.with_name("messages_sale.json")
    if sale.exists():
        ds = json.loads(sale.read_text(encoding="utf-8"))
        _MSG.update({m["id"]: m for s in ds["sections"] if not s.get("override") for m in s["messages"]})
        # 売買版のサーバー（APP_MODE=sale）だけ、賃貸版と同じIDの文面を売買の言い方に差し替える（賃貸版では読まない）
        import config
        if config.APP_MODE == "sale":
            for s in ds["sections"]:
                if s.get("override"):
                    for m in s["messages"]:
                        _MSG[m["id"]] = {**_MSG.get(m["id"], {}), **m}


load()


class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def _defaults():
    """売買版だけ、料金の件数・回数を文面の差し込みの初期値にする（{ライトの件数} など）。賃貸版は何もしない。"""
    import config
    if config.APP_MODE != "sale":
        return {}
    import naiken
    return {"ライトの件数": config.LIGHT_SHEETS, "MAXの件数": config.MAX_SHEETS,
            "ライトのFAX": naiken.LIMITS["light"][0], "MAXのFAX": naiken.LIMITS["unlimited"][0]}


def fill(t, **kw):
    if "{" in t:
        d = _defaults()
        if d:
            kw = {**d, **kw}
    t = t.format_map(_Safe(**kw))
    # 空の差し込みでできた空行の重なりを1つにする
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def raw(mid, **kw):
    return fill(_MSG[mid]["text"], **kw)


def _action(b, urls, kw=None):
    a, label = b["action"], fill(b["label"], **(kw or {}))
    if a == "camera":
        return {"type": "camera", "label": label[:20]}
    if a == "cameraRoll":
        return {"type": "cameraRoll", "label": label[:20]}
    if a.startswith("postback "):
        data = fill(a.split(" ", 1)[1], **(kw or {}))  # 「naiken=from_last&doc={資料ID}」など
        return {"type": "postback", "label": label[:20], "data": data, "displayText": label}
    if a.startswith("uri "):
        target = a.split(" ", 1)[1]
        url = urls.get(label) or urls.get(target) or (target if target.startswith("http") else None)
        if not url:
            return None
        return {"type": "uri", "label": label[:40], "uri": url}
    return None


def build(mid, urls=None, extra_buttons=None, append="", text=None, drop=(), buttons=None, **kw):
    """文面IDからLINEのメッセージを1つ作る。urls はボタンのラベル→URL。text で文を差し替えられる。
    drop は出さないボタンの action（例 "postback store=start"）、buttons でボタンを丸ごと差し替え。"""
    m = _MSG[mid]
    text = text if text is not None else raw(mid, **kw)
    if append:
        text = text + "\n\n" + append
    buttons = list(m.get("buttons", []) if buttons is None else buttons) + list(extra_buttons or [])
    buttons = [b for b in buttons if b["action"] not in drop]
    acts = [a for a in (_action(b, urls or {}, kw) for b in buttons) if a]
    if any(a["type"] == "uri" for a in acts):
        return _card(text, acts)
    msg = line.text(text)
    if acts:
        msg["quickReply"] = {"items": [{"type": "action", "action": a} for a in acts[:13]]}
    return msg


def _card(text, acts):
    """文とボタンのカード。カメラなどカードに置けない動作は除く。"""
    btns = []
    for i, a in enumerate(a for a in acts if a["type"] in ("uri", "postback")):
        b = {"type": "button", "height": "sm", "style": "primary" if i == 0 else "secondary",
             "action": {k: v for k, v in a.items() if k != "displayText" or a["type"] == "postback"}}
        if i == 0:
            b["color"] = line.BLUE
        b["action"]["label"] = b["action"]["label"][:40]
        btns.append(b)
    return {"type": "flex", "altText": text[:400], "contents": {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": text, "wrap": True, "size": "sm", "color": "#333333"}]},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": btns},
    }}


def note(mid, **kw):
    """ほかの文の後ろに足す一文（のこり枚数の案内など）。"""
    if mid not in _MSG:
        return ""
    t = raw(mid, **kw)
    return "" if t.startswith("（") else t


def buttons_of(mid):
    return list(_MSG.get(mid, {}).get("buttons", []))
