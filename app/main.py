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
from pydantic import BaseModel

import api
import billing
import bot
import config
import delivery
import line
import store
import usage

line.on_push = usage.on_push  # push の通数を数える・上限で送れなかったお知らせをためる

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")
app = FastAPI(title="1分資料", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _setup_webhooks():
    """起動したら、Stripe と LINE の通知先がこのサーバーを向いているか確かめる（手作業を減らすため）。"""
    import threading

    def run():
        import richmenu
        for name, fn in (("prices", billing.ensure_prices), ("stripe", billing.ensure_webhook), ("line", line.ensure_webhook), ("richmenu", richmenu.ensure),
                         ("descriptor", billing.ensure_descriptors)):
            try:
                fn()
            except Exception:
                logging.getLogger("main").exception("%s の通知先の設定に失敗", name)
    threading.Thread(target=run, daemon=True).start()
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


NAMES = {"light": "ライトプラン（月額1,480円（税込）・募集図面 月50件分）", "unlimited": "使い放題プラン（月額2,980円（税込））",
         "light_year": "ライトプラン 年払い（年額14,800円（税込）・募集図面 月50件分）",
         "unlimited_year": "使い放題プラン 年払い（年額29,800円（税込））"}


def _plan(key):
    """URLの名前から (プラン, 年払いか)。例 light_year → ("light", True)"""
    return key.removesuffix("_year"), key.endswith("_year")


@app.get("/pay/{plan}")
def pay(plan: str, t: str = ""):
    uid = _uid(t)
    if plan == "faxpack":
        return RedirectResponse(billing.fax_pack_checkout(uid), status_code=303)
    if plan == "faxrecv":
        return RedirectResponse(billing.faxrecv_pack_checkout(uid), status_code=303)
    u = store.get_user(uid)
    if plan == "store":
        import campaign
        if u.get("shop_code"):
            return page("お申し込み済みです", f"<p>{html.escape(u.get('shop_name') or '')}の店舗まとめ割をご利用中です。</p>")
        if not campaign.store_open():
            return page("受付を終了しました", "<p>店舗まとめ割のお申し込みは終了しました。お一人ずつのプランは、いつでもお申し込みいただけます。</p>")
        url = campaign.checkout_url(uid)
        if not url:
            return page("もう一度お試しください", "<p>お申し込みの内容が見つかりませんでした。LINEの「店舗まとめ割」から、もう一度お進みください。</p>")
        return RedirectResponse(url, status_code=303)
    if plan not in NAMES:
        raise HTTPException(404)
    if u.get("shop_code") or u.get("plan_source") == "group":
        return page("店舗まとめ割をご利用中です", f"""<p>{html.escape(u.get('shop_name') or '店舗')}の店舗まとめ割でご利用中のため、
個人のプランのお申し込みはいりません。人数やプランのご相談は、店舗の代表の方にお願いします。</p>""")
    p, yearly = _plan(plan)
    if u.get("stripe_subscription"):
        if (p, yearly) == (u.get("plan"), u.get("billing_interval") == "year"):
            return page("ご契約中です", f"<p>すでに{NAMES[plan]}をご利用中です。</p>")
        if billing.change_kind(u, p, yearly) == "up":
            note = "今日から切り替わり、今の期間の残りの日数分の差額を、ご登録のカードにすぐご請求します。"
        else:
            note = (f"次の更新日（{store.billing_renew_date(u)}）から切り替わります。それまでは今のプランのままお使いいただけます"
                    "（日割りの返金はありません）。")
        return page("ご契約内容の変更", f"""<p>{NAMES[plan]}に変更します。{note}</p>
<form method=post action="/pay/{plan}/change?t={html.escape(t)}"><button>変更する</button></form>""")
    return RedirectResponse(billing.checkout(uid, p, yearly), status_code=303)


@app.post("/pay/{plan}/change")
def pay_change(plan: str, t: str = ""):
    uid = _uid(t)
    p, yearly = _plan(plan)
    if plan not in NAMES or not billing.can_change(store.get_user(uid), p, yearly):
        raise HTTPException(404)
    u = store.get_user(uid)
    kind, val = billing.change_plan(uid, p, yearly)
    new_name = NAMES[plan].split("（")[0]
    if kind == "up":
        u2 = store.get_user(uid)
        bot.notify(uid, "plan_upgraded", {"新しいプラン名": new_name, "差額": f"{val:,}",
                                          "更新日": store.billing_renew_date(u2), "新しい料金": billing.YEN[(p, yearly)]})
        return page("変更しました", f"<p>{new_name}に変更しました。LINEに戻って、そのままお使いください。</p>")
    d = val.astimezone(store.JST)
    when = f"{d.month}月{d.day}日"
    bot.notify(uid, "plan_downgrade_scheduled", {"更新日": when, "新しいプラン名": new_name,
                                                 "新しい料金": billing.YEN[(p, yearly)], "プラン名": billing.plan_name(u)})
    return page("承りました", f"<p>{when}から{new_name}に切り替わります。それまでは今のプランのままお使いいただけます。</p>")


@app.get("/portal")
def portal(t: str = ""):
    uid = _uid(t)
    url = billing.portal(uid)
    if not url:
        return page("料金プラン", f"""<p>まだ有料プランのお申し込みはありません。</p>
<a class=btn href="/pay/light?t={html.escape(t)}">ライトプラン（月額1,480円（税込）・募集図面 月50件分）</a>
<a class=btn href="/pay/unlimited?t={html.escape(t)}">使い放題プラン（月額2,980円（税込））</a>
<a class=btn href="/pay/light_year?t={html.escape(t)}">ライトプラン 年払い（年額14,800円（税込））</a>
<a class=btn href="/pay/unlimited_year?t={html.escape(t)}">使い放題プラン 年払い（年額29,800円（税込））</a>""")
    return RedirectResponse(url, status_code=303)


@app.get("/card")
def card(t: str = ""):
    import monitor
    uid = _uid(t)
    return RedirectResponse(monitor.card_checkout(uid), status_code=303)


@app.post("/cron/daily")
def cron_daily(x_cron_key: str = Header("")):
    """Cloud Scheduler から毎日呼ぶ（モニターのお知らせなど）。"""
    import hmac as _hmac
    if not _hmac.compare_digest(x_cron_key, config.env("CRON_KEY") or config.APP_SECRET):
        raise HTTPException(403)
    import monitor
    renew = 0
    for uid, kw in billing.annual_renew_notices():
        bot.notify(uid, "plan_annual_renew", kw)
        renew += 1
    import campaign
    store_notices = campaign.daily()
    for uid, mid, kw in store_notices:
        bot.notify(uid, mid, kw)
    import retention
    import usage
    try:
        usage.daily_report()  # 毎朝9時の日報【1分資料 日報】
    except Exception:
        log.exception("日報")
    import growth
    if store.now().astimezone(store.JST).weekday() == 0:
        try:
            growth.weekly_report()  # 毎週月曜9時の週報【1分資料 週報】
        except Exception:
            log.exception("週報")
    return {"monitor_sent": monitor.daily(), "annual_renew_sent": renew, "store_notices": len(store_notices),
            "purged": retention.daily()}


@app.post("/cron/often")
def cron_often(x_cron_key: str = Header("")):
    """Cloud Scheduler から5分ごとに呼ぶ（受取りFAXの確認のお知らせ、FAXの送信結果の問い合わせ、預かった募集図面の削除、お支払いの猶予切れ）。"""
    import hmac as _hmac
    if not _hmac.compare_digest(x_cron_key, config.env("CRON_KEY") or config.APP_SECRET):
        raise HTTPException(403)
    import usage
    import growth
    return {"usage_alerts": usage.check(), "growth_sent": growth.run_due(), "fax_results": delivery.poll_faxes(), "recvfax_remind": bot.remind_recvfax(), "pending_purged": bot.purge_pending(),
            "grace_ended": billing.end_grace()}


@app.get("/admin/usage")
def admin_usage(key: str = ""):
    """LINE push・メールの通数（読み取り専用、合言葉 ADMIN_USAGE_KEY）。"""
    import hmac as _hmac
    import usage
    want = config.env("ADMIN_USAGE_KEY")
    if not (want and _hmac.compare_digest(key, want)):
        raise HTTPException(403)
    return usage.stats()


def _ops_key(key):
    import hmac as _hmac
    want = config.env("ADMIN_OPS_KEY")
    if not (want and _hmac.compare_digest(key or "", want)):
        raise HTTPException(403)


class BroadcastIn(BaseModel):
    id: str                 # 文面ID（ops_outage・ops_outage_mail など）
    to: str = "line"        # line（LINE全員）か mail（メール全員）
    values: dict = {}       # 差し込み（発生日時・影響など）
    send: bool = False      # false なら送らずに、人数・残りの通数・見本だけ返す


@app.post("/admin/broadcast")
def admin_broadcast(body: BroadcastIn, key: str = ""):
    """障害・メンテナンスのお知らせの一斉送信（合言葉 ADMIN_OPS_KEY）。まず send=false で確かめてから送る。"""
    import threading
    import texts
    import usage
    _ops_key(key)
    if body.to not in ("line", "mail") or body.id not in texts._MSG or not body.id.startswith("ops_"):
        raise HTTPException(422, "id は ops_ で始まる文面ID、to は line か mail")
    plan = usage.broadcast_plan(body.id, body.to, body.values)
    if not body.send:
        return plan
    threading.Thread(target=usage.broadcast, args=(body.id, body.to, body.values), daemon=True).start()
    return {**plan, "started": True}


class OutageIn(BaseModel):
    on: bool
    values: dict = {}       # ops_outage_reply の差し込み（影響・送った募集図面の扱い）


@app.post("/admin/outage")
def admin_outage(body: OutageIn, key: str = ""):
    """「障害中」スイッチ。オンの間は、利用者からのメッセージに ops_outage_reply を応答で返す。"""
    import usage
    _ops_key(key)
    usage.set_outage(body.on, body.values)
    return {"on": body.on}


@app.get("/admin/referrals/hold")
def admin_referral_hold(key: str = ""):
    """紹介が1か月に5件を超えて保留しているものの一覧（合言葉 ADMIN_OPS_KEY）。"""
    import referral
    _ops_key(key)
    return {"hold": referral.held()}


@app.post("/admin/referrals/{referred}/{action}")
def admin_referral_decide(referred: str, action: str, key: str = ""):
    """保留の判断。action は release（特典を渡す）か void（渡さない）。"""
    import referral
    _ops_key(key)
    if action not in ("release", "void"):
        raise HTTPException(422)
    notices = referral.decide(referred, action == "release")
    if notices is None:
        raise HTTPException(404, "保留中の紹介が見つかりません")
    for uid, mid, kw in notices:
        bot.notify(uid, mid, kw)
    return {"ok": True, "action": action, "notices": [m for _, m, _ in notices]}


class OptoutIn(BaseModel):
    number: str
    note: str = ""          # 例：「〇〇管理 様より電話で依頼（10/29）」


@app.get("/admin/fax-optout")
def admin_optout_list(key: str = ""):
    """元付業者さんから「FAXを止めて」と依頼された番号の一覧（合言葉 ADMIN_OPS_KEY）。"""
    import naiken
    _ops_key(key)
    return {"optout": naiken.optout_list()}


@app.post("/admin/fax-optout")
def admin_optout_add(body: OptoutIn, key: str = ""):
    """送信停止リストに足す。この番号には内見申込書のFAXを送らず、利用者にはお電話などをお願いする。"""
    import naiken
    _ops_key(key)
    n = naiken.optout_add(body.number, body.note)
    if not n:
        raise HTTPException(422, "FAX番号を確認してください")
    return {"ok": True, "id": n}


@app.delete("/admin/fax-optout")
def admin_optout_remove(number: str, key: str = ""):
    import naiken
    _ops_key(key)
    return {"removed": naiken.optout_remove(number)}


@app.post("/admin/line/webhook")
async def admin_line_webhook(request: Request, x_line_signature: str = Header("")):
    """運営専用LINE「1分資料 運営通知」の Webhook。友だちになった運営の方のIDを覚える。"""
    import json as _json
    import usage
    body = await request.body()
    if not usage.admin_line_webhook(body, x_line_signature, _json.loads(body or b"{}").get("events", [])):
        raise HTTPException(400)
    return {"ok": True}


@app.get("/paid")
def paid(t: str = ""):
    return page("ありがとうございました", f"""<p>お手続きが完了しました。LINEに戻って、そのままお使いください。</p>
<a class=btn href="https://line.me/R/">LINEに戻る</a>""")


@app.get("/canceled")
def canceled():
    return page("お申し込みを中止しました", "<p>お支払いは行われていません。LINEに戻って、いつでもお申し込みいただけます。</p>")


# ---------- 資料のファイル ----------

@app.get("/n/{fid}/{token}/{name}")
def naiken_file(fid: str, token: str, name: str):
    import naiken
    data = naiken.read_file(fid, token, name)
    if data == "expired":
        return page("リンクの期限が切れました", "<p>内見申込書を見られるのは7日間です。</p>", 410)
    if not data:
        raise HTTPException(404)
    return Response(data, media_type="application/pdf" if name.endswith(".pdf") else "image/png",
                    headers={"Cache-Control": "private, max-age=3600"})

@app.get("/f/{jid}/{token}/{name}")
def file(jid: str, token: str, name: str):
    data = store.read_file(jid, token, name)
    if data == "expired":
        return page("リンクの期限が切れました", "<p>お手数ですが、もう一度募集図面を送って資料を作り直してください。</p>", 410)
    if not data:
        raise HTTPException(404)
    headers = {"Cache-Control": "private, max-age=3600"}
    if name.endswith(".pdf"):
        headers["Content-Disposition"] = 'inline; filename="shiryou.pdf"'
    return Response(data, media_type=store.FILES[name], headers=headers)
