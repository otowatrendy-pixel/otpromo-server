"""1分資料のサーバー（Cloud Run）。

入口は2つ：LINE の Webhook（/line/webhook）と、アプリ版の REST API（/v1/...）。
"""
import html
import json
import logging
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import api
import billing
import bot
import config
import line
import store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")
app = FastAPI(title="1分資料", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(api.router)


@app.get("/")
def health():
    return {"ok": True, "service": "1分資料", "model": config.MODEL}


# ---------- LINE ----------

@app.post("/line/webhook")
async def line_webhook(request: Request, x_line_signature: str = Header("")):
    body = await request.body()
    if not line.verify(body, x_line_signature):
        raise HTTPException(400, "bad signature")
    bot.handle_events(json.loads(body))
    return {"ok": True}


# ---------- Stripe ----------

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header("")):
    payload = await request.body()
    try:
        notices = billing.handle_webhook(payload, stripe_signature)
    except Exception as e:
        log.warning("Stripe webhook 失敗 %r", e)
        raise HTTPException(400, "bad webhook")
    for uid, mid, kw in notices:
        try:
            bot.notify(uid, mid, kw)
        except Exception:
            log.exception("LINEへのお知らせに失敗")
    return {"ok": True}


def page(title, body, status=200):
    """支払いの前後などに出す、スマホ向けの小さなページ。"""
    return HTMLResponse(status_code=status, content=f"""<!doctype html><html lang=ja><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{html.escape(title)}｜1分資料</title>
<style>body{{font-family:"Hiragino Sans","Noto Sans JP",sans-serif;margin:0;background:#F3F6FB;color:#222;
word-break:auto-phrase;line-break:strict}}main{{max-width:480px;margin:0 auto;padding:32px 20px}}
h1{{color:#1A56A8;font-size:20px}}p{{line-height:1.8}}.card{{background:#fff;border-radius:12px;padding:20px;
box-shadow:0 1px 4px rgba(0,0,0,.08)}}button,.btn{{display:block;width:100%;box-sizing:border-box;text-align:center;
background:#1A56A8;color:#fff;border:0;border-radius:8px;padding:14px;font-size:16px;margin-top:16px;
text-decoration:none}}small{{color:#666}}</style><main><div class=card><h1>{html.escape(title)}</h1>{body}</div>
<p><small>1分資料（otpromo.com）</small></p></main></html>""")


def _uid(t):
    uid = billing.unsign(t or "")
    if not uid or not store.get_user(uid):
        raise HTTPException(403, "リンクの期限が切れています。LINEのメニューから開き直してください。")
    return uid


NAMES = {"light": "ライトプラン（月額1,480円・月20枚）", "unlimited": "使い放題プラン（月額2,980円）",
         "fax": "FAXオプション（月額500円）"}


@app.get("/pay/{plan}")
def pay(plan: str, t: str = ""):
    uid = _uid(t)
    if plan not in NAMES:
        raise HTTPException(404)
    u = store.get_user(uid)
    if u.get("stripe_subscription"):
        cur = u.get("plan")
        if plan == cur or (plan == "fax" and u.get("fax_option")):
            return page("ご契約中です", f"<p>すでに{NAMES[plan]}をご利用中です。</p>")
        return page("ご契約内容の変更", f"""<p>{NAMES[plan]}に変更します。差額は日割りで次回のご請求に反映されます（価格は税別）。</p>
<form method=post action="/pay/{plan}/change?t={html.escape(t)}"><button>変更する</button></form>""")
    if plan == "fax":
        return page("FAXオプション", f"""<p>FAXオプションは、ライトプランか使い放題プランに追加してお申し込みいただけます。</p>
<a class=btn href="/pay/light/fax?t={html.escape(t)}">ライト＋FAXオプション</a>
<a class=btn href="/pay/unlimited/fax?t={html.escape(t)}">使い放題＋FAXオプション</a>""")
    return RedirectResponse(billing.checkout(uid, plan), status_code=303)


@app.get("/pay/{plan}/fax")
def pay_with_fax(plan: str, t: str = ""):
    uid = _uid(t)
    return RedirectResponse(billing.checkout(uid, plan, fax=True), status_code=303)


@app.post("/pay/{plan}/change")
def pay_change(plan: str, t: str = ""):
    uid = _uid(t)
    if plan == "fax":
        billing.change_plan(uid, fax=True)
    elif plan in ("light", "unlimited"):
        billing.change_plan(uid, plan=plan)
    else:
        raise HTTPException(404)
    u = store.get_user(uid)
    if plan != "fax":
        bot.notify(uid, "plan_started", {})
    return page("変更しました", f"<p>{NAMES[plan]}に変更しました。LINEに戻って、そのままお使いください。</p>")


@app.get("/portal")
def portal(t: str = ""):
    uid = _uid(t)
    url = billing.portal(uid)
    if not url:
        return page("料金プラン", f"""<p>まだ有料プランのお申し込みはありません。</p>
<a class=btn href="/pay/light?t={html.escape(t)}">ライトプラン（月額1,480円・月20枚）</a>
<a class=btn href="/pay/unlimited?t={html.escape(t)}">使い放題プラン（月額2,980円）</a><p><small>価格は税別です。</small></p>""")
    return RedirectResponse(url, status_code=303)


@app.get("/paid")
def paid(t: str = ""):
    return page("ありがとうございました", f"""<p>お手続きが完了しました。LINEに戻って、そのままお使いください。</p>
<a class=btn href="https://line.me/R/">LINEに戻る</a>""")


@app.get("/canceled")
def canceled():
    return page("お申し込みを中止しました", "<p>お支払いは行われていません。LINEに戻って、いつでもお申し込みいただけます。</p>")


# ---------- 資料のファイル ----------

@app.get("/f/{jid}/{token}/{name}")
def file(jid: str, token: str, name: str):
    data = store.read_file(jid, token, name)
    if data == "expired":
        return page("リンクの期限が切れました", "<p>お手数ですが、もう一度マイソクを送って資料を作り直してください。</p>", 410)
    if not data:
        raise HTTPException(404)
    headers = {"Cache-Control": "private, max-age=3600"}
    if name.endswith(".pdf"):
        headers["Content-Disposition"] = 'inline; filename="shiryou.pdf"'
    return Response(data, media_type=store.FILES[name], headers=headers)
