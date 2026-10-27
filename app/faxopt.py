"""受け取り用FAXオプション（月額990円税込）。

できあがった資料のうち、帯替え図面＋初期費用概算書の2ページを白黒で利用会社のFAXへ送る。
月40ページ（マイソク20件分）まで。超えた分は1ページ15円を、次の請求に足す（Stripe の請求項目）。
数える区切りは資料の件数と同じ（有料は請求期間、それ以外は暦の月）。内見申込FAXの回数とは別枠。
"""
import io
import logging

import config
import store

log = logging.getLogger("faxopt")
PAGES = int(config.env("FAXOPT_PAGES", "40"))          # 月のページ数
OVER_YEN = int(config.env("FAXOPT_OVER_YEN", "15"))    # 超えた分の1ページの料金（税込）
PER_DOC = 2                                             # 1件で送るページ数
NEAR = 6                                                # のこりがこれ以下になったら案内する


def used(u):
    c = u.get("faxopt_count") or {}
    return c.get(store.period_key(u), 0)


def left(u):
    return max(0, PAGES - used(u))


def paused(u):
    """「今月はLINEだけにする」を選んだ期間中か。"""
    return u.get("faxopt_paused") == store.period_key(u)


def play_billed(u):
    """アプリ（Google Play）で申し込んだオプションか。Play では超過分の従量課金ができないので40ページで止める。"""
    return u.get("fax_option_source") == "google_play"


def can_send(u):
    """いまFAXで送ってよいか（加入・一時停止・アプリ版の上限）。"""
    if paused(u):
        return False
    if play_billed(u) and left(u) < PER_DOC:
        return False
    return True


def pause(uid):
    u = store.get_user(uid)
    store.update_user(uid, {"faxopt_paused": store.period_key(u)})


def record(uid, pages=PER_DOC):
    """送ったページを数える。戻り値は (送ったあとの今月のページ数, 今回の超過ページ数)。"""
    out = {}

    def fn(u):
        k = store.period_key(u)
        c = {k: (u.get("faxopt_count") or {}).get(k, 0)}  # 古い期間の数は残さない
        before = c[k]
        c[k] = before + pages
        u["faxopt_count"] = c
        out["n"], out["over"] = c[k], max(0, c[k] - max(before, PAGES))
    store.db().update_tx("users", uid, fn)
    if out["over"] and not play_billed(store.get_user(uid)):
        try:
            import billing
            billing.fax_overage(uid, out["over"], OVER_YEN)
        except Exception:
            log.exception("FAXの超過分を請求に足せませんでした uid=%s pages=%s", uid, out["over"])
    return out["n"], out["over"]


def note(n):
    """送ったあとに資料の文へ添える文面ID（なければ None）。n は送ったあとの今月のページ数。"""
    if n > PAGES and n - PER_DOC <= PAGES:
        return "faxopt_over"  # 超えた最初の1回
    if n <= PAGES and PAGES - n <= NEAR:
        return "faxopt_near"
    return None


def build_pdf(company, job):
    """資料から、FAX用の2ページ（白黒）を作る。帯替え図面は保存してある画像を使う。"""
    from PIL import Image

    import core
    import render
    from schema import Result
    ob = Image.open(io.BytesIO(store.db().get_file(f"jobs/{job['_id']}/obikae.png")))
    result = Result.model_validate(job["result"])
    company = {**core.DEFAULT_COMPANY, **(company or {}), "テーマ色": "#222222"}
    html_text = render.build_html(result, render.png_bytes(render.fax_image(ob)), company,
                                  sections=render.FAX_SECTIONS, bw=True)
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "fax.pdf"
        render.to_pdf(html_text, out)
        return out.read_bytes()
