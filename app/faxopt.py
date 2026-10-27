"""受け取り用FAX（回数券）。

できあがった資料のうち、帯替え図面＋初期費用概算書の2枚（白黒）を、利用会社自身のFAX機へ送る。
回数券は10回1,000円（税込、1回100円）、購入から3か月有効。1回＝マイソク1件分。
LINE版は Stripe の都度払い（lookup_key fax_receive_10）、アプリ版は Google Play の消費型アイテム。
内見申込書FAX（元付業者へ送るもの）の回数とは別に数える。のこり0回になったら送らず、LINEで購入をご案内する。
"""
import io
import logging
import tempfile
from datetime import timedelta
from pathlib import Path

import config
import store

log = logging.getLogger("faxopt")
PACK_SIZE = int(config.env("FAXRECV_PACK_SIZE", "10"))
PACK_DAYS = int(config.env("FAXRECV_PACK_DAYS", "90"))
PACK_YEN = int(config.env("FAXRECV_PACK_YEN", "1000"))
LOW = 3  # のこりがこれ以下になったら案内する（recvfax_low）


def _live(u):
    t = store.now()
    return sorted([p for p in (u.get("faxrecv_packs") or []) if p["exp"] > t and p["used"] < p["n"]],
                  key=lambda p: p["exp"])


def left(u):
    return sum(p["n"] - p["used"] for p in _live(u))


def next_expiry(u):
    live = _live(u)
    return live[0]["exp"] if live else None


def can_send(u):
    return left(u) > 0


def add_pack(uid, n=PACK_SIZE):
    def fn(u):
        t = store.now()
        u["faxrecv_packs"] = [p for p in (u.get("faxrecv_packs") or []) if p["exp"] > t and p["used"] < p["n"]] + [
            {"n": n, "used": 0, "at": t, "exp": t + timedelta(days=PACK_DAYS)}]
    store.db().update_tx("users", uid, fn)


def use(uid):
    """1回分を使う（期限の近い回数券から）。戻り値は使ったあとのこり回数。使えなければ None。"""
    out = {}

    def fn(u):
        live = _live(u)
        if not live:
            out["left"] = None
            return
        live[0]["used"] += 1
        out["left"] = sum(p["n"] - p["used"] for p in live)
    store.db().update_tx("users", uid, fn)
    return out["left"]


def refund(uid):
    """送信に失敗したときに1回分を戻す。"""
    def fn(u):
        used = sorted([p for p in (u.get("faxrecv_packs") or []) if p["used"] > 0 and p["exp"] > store.now()],
                      key=lambda p: p["exp"])
        if used:
            used[0]["used"] -= 1
    store.db().update_tx("users", uid, fn)


def build_pdf(company, job):
    """資料から、FAX用の2枚（白黒）を作る。帯替え図面は保存してある画像を使う。"""
    from PIL import Image

    import core
    import render
    from schema import Result
    ob = Image.open(io.BytesIO(store.db().get_file(f"jobs/{job['_id']}/obikae.png")))
    result = Result.model_validate(job["result"])
    company = {**core.DEFAULT_COMPANY, **(company or {}), "テーマ色": "#222222"}
    html_text = render.build_html(result, render.png_bytes(render.fax_image(ob)), company,
                                  sections=render.FAX_SECTIONS, bw=True)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "fax.pdf"
        render.to_pdf(html_text, out)
        return out.read_bytes()


def send_job(uid, u, jid, job, company):
    """回数券を1回使って、資料の2枚を利用会社のFAXへ送る。戻り値はのこり回数（送れなければ None）。"""
    number = (u.get("delivery") or {}).get("fax")
    import delivery
    if not (number and delivery.fax_ready() and can_send(u)):
        return None
    pdf = build_pdf(company, {**job, "_id": jid})
    n = use(uid)
    if n is None:
        return None
    if not delivery.send_fax(number, pdf):
        refund(uid)
        return None
    return n
