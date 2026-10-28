"""LINEのリッチメニュー（トーク画面の下のメニュー）を、起動時に自動で用意する。

画像は app/static/richmenu-*.png（tools/make_richmenu.py で messages.json から作る）。
画像かボタンが変わったときだけ作り直し、IDは Firestore meta/richmenu にしまう。
登録前の方には既定のメニュー（登録をはじめる・使い方）、登録済みの方には大きいメニューを個別に付ける。
"""
import hashlib
import json
import logging
from pathlib import Path

import requests

import config
import line
import store

log = logging.getLogger("richmenu")
STATIC = Path(__file__).parent / "static"
DATA_API = "https://api-data.line.me/v2/bot"
_ids = {}


def _spec():
    return json.loads((Path(__file__).parent / "messages.json").read_text())["richmenu"]


def _action(a):
    kind, _, rest = a["action"].partition(" ")
    if kind == "uri":
        return {"type": "uri", "label": a["label"][:20], "uri": rest}
    return {"type": "postback", "label": a["label"][:20], "data": rest, "displayText": a["label"]}


def _bounds(name, n):
    if name == "registered":  # 3列×2段
        xs, w, h = [0, 833, 1667], [833, 834, 833], 843
        return [{"x": xs[i % 3], "y": (i // 3) * h, "width": w[i % 3], "height": h} for i in range(n)]
    return [{"x": 0, "y": 0, "width": 1667, "height": 843}, {"x": 1667, "y": 0, "width": 833, "height": 843}]


def _body(name, sp):
    size = {"width": 2500, "height": 1686 if name == "registered" else 843}
    areas = [{"bounds": b, "action": _action(a)} for b, a in zip(_bounds(name, len(sp["areas"])), sp["areas"])]
    return {"size": size, "selected": name == "unregistered", "name": f"1分資料 {name}",
            "chatBarText": sp.get("chatBarText", "メニュー"), "areas": areas}


def ensure():
    """メニューが今の画像・ボタンと同じか確かめ、違えば作り直す。"""
    if not config.LINE_CHANNEL_ACCESS_TOKEN:
        return
    sp = _spec()
    bodies = {n: _body(n, sp[n]) for n in ("registered", "unregistered")}
    imgs = {n: (STATIC / f"richmenu-{n}.png").read_bytes() for n in bodies}
    ver = hashlib.sha256(json.dumps(bodies, ensure_ascii=False, sort_keys=True).encode()
                         + b"".join(imgs[n] for n in sorted(imgs))).hexdigest()[:16]
    meta = store.db().get("meta", "richmenu") or {}
    if meta.get("version") == ver and meta.get("registered") and meta.get("unregistered"):
        _ids.update(registered=meta["registered"], unregistered=meta["unregistered"])
        return
    new = {}
    for n, body in bodies.items():
        r = requests.post(f"{line.API}/richmenu", headers=line._h(), json=body, timeout=15)
        r.raise_for_status()
        rid = r.json()["richMenuId"]
        h = {**line._h(), "Content-Type": "image/png"}
        requests.post(f"{DATA_API}/richmenu/{rid}/content", headers=h, data=imgs[n], timeout=30).raise_for_status()
        new[n] = rid
    requests.post(f"{line.API}/user/all/richmenu/{new['unregistered']}", headers=line._h(), timeout=10).raise_for_status()
    for n in ("registered", "unregistered"):  # 古いメニューは消す
        if meta.get(n):
            requests.delete(f"{line.API}/richmenu/{meta[n]}", headers=line._h(), timeout=10)
    store.db().set("meta", "richmenu", {"version": ver, **new})
    _ids.update(new)
    log.info("リッチメニューを作り直しました %s", new)


def menu_id(registered):
    if not _ids:
        meta = store.db().get("meta", "richmenu") or {}
        _ids.update({k: meta[k] for k in ("registered", "unregistered") if meta.get(k)})
    return _ids.get("registered" if registered else "unregistered")
