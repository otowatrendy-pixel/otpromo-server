"""アプリ版（Android）の REST API。ログインは Firebase Authentication の IDトークン。

POST /v1/jobs            画像かPDF（multipart の file）→ {job_id}
GET  /v1/jobs/{id}       → {status, pdf_url（全部）, customer_pdf_url（お客様向け）, staff_pdf_url（社内向け）, obikae_url, texts, error}
PUT  /v1/jobs/{id}/atena {"atena":"山田 太郎"} 初期費用概算書の宛名（""で空欄。でき上がり後なら作り直す。409 失敗した資料）
POST /v1/jobs/{id}/fix   {"request":"右下の連絡先を消して"} 業者向け情報の消し直し（無料・件数に数えない）
GET  /v1/me              → {plan, plan_source, remaining, free_left, registered, ask_cancel_reason, company}
POST /v1/me/cancel-reason {"reason":"price|nouse|accuracy|other_service|other"} 解約の理由
PUT  /v1/jobs/{id}/choice {"index":0} 条件が2通り以上ある図面で、どれで概算書を作るか（GET の choices・choice）
PUT  /v1/me/company      会社情報（帯・色・書体・受け取り方・宛名を聞く）。担当者名・携帯は必須（422 name_required/mobile_check/name_check、429 name_locked）
POST /v1/billing/google/verify, /v1/billing/google/rtdn  … Google Play の課金
POST /v1/billing/apple/verify, /v1/billing/apple/notify  … App Store の課金（iPhone版）
"""
import logging

from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel

import bot
import config
import core
import delivery
import faxopt
import store
import texts

router = APIRouter(prefix="/v1")
log = logging.getLogger("api")


def _firebase_uid(authorization: str, want_email=False):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "ログインが必要です")
    token = authorization[7:]
    if config.STORE == "local" and token.startswith("dev:"):  # 手元の確認用
        return token[4:]
    from google.auth.transport import requests as greq
    from google.oauth2 import id_token
    try:
        info = id_token.verify_firebase_token(token, greq.Request(), audience=config.FIREBASE_PROJECT)
    except Exception:
        raise HTTPException(401, "ログインの確認に失敗しました")
    return info["sub"] if not want_email else (info["sub"], info.get("email"))


def _me(authorization):
    if config.STORE == "local" and authorization.startswith("Bearer dev:"):
        fb, email = _firebase_uid(authorization), None
        if fb.startswith("review:"):  # 手元の確認用
            email = config.REVIEW_EMAILS[0]
    else:
        fb, email = _firebase_uid(authorization, want_email=True)
    uid, u = store.user_by_firebase(fb)
    import review
    if review.is_review_email(email) or review.is_review(u):
        u = review.ensure(uid, u)
    return uid, u


@router.get("/me")
def me(authorization: str = Header("")):
    uid, u = _me(authorization)
    import naiken
    q = store.quota(u)
    month, packs, day = naiken.remaining(u)
    return {"plan": q["plan"], "plan_source": u.get("plan_source"), "remaining": q["remaining"],
            "plan_period": "year" if q["plan"] != "free" and u.get("billing_interval") == "year" else "month",
            "naiken_month_left": month, "naiken_pack_left": packs,
            "naiken_day_left": day,
            "free_left": q["free_left"], "registered": u.get("registered", False),
            # 解約済みでまだ理由を受け取っていないとき true。アプリはこのときだけ1回聞く
            "ask_cancel_reason": bool(u.get("ask_cancel_reason")),
            "company": u.get("company") or {}, "delivery": u.get("delivery") or {},
            # 売買版：アプリは sale_enabled が true のときだけ売買の画面・設定を出す
            "sale_enabled": _sale_on(uid), "use_kind": u.get("use_kind"),
            "sale_needs_setup": _sale_on(uid) and __import__("sale_bot").needs_setup(u),
            # 受け取り用FAX（回数券 10回1,000円・購入から3か月）。next_expiry は一番近い期限
            # 番号が未登録なら null
            # お試し中の受け取り用FAXの無料のこり（最初の3件まで。有料プランでは null）
            "fax_receive_trial_left": max(0, bot.TRIAL_FAX - (u.get("trial_fax_used") or 0))
            if (u.get("plan") or "free") == "free" else None,
            "fax_receive": {"number": (u.get("delivery") or {}).get("fax"), "left": faxopt.left(u),
                            "next_expiry": faxopt.next_expiry(u).isoformat() if faxopt.next_expiry(u) else None}
            if (u.get("delivery") or {}).get("fax") else None,
            # 紹介：自分のコード・使った紹介コード・特典を渡した紹介の数
            "referral": _referral_me(uid),
            # 利用規約：同意した版と今の版。needs_agree が true なら規約の画面を出して PUT /v1/me/terms
            "terms": terms_state(u),
            # 予約中のプラン変更（下げる・払い方の変更は次の更新日から）
            "pending_change": _pending(u)}


def terms_state(u):
    agreed = u.get("terms_version") or (config.TERMS_VERSION if u.get("agreed_at") else None)
    return {"url": config.TERMS_URL, "agreed_version": agreed, "current_version": config.TERMS_VERSION,
            "needs_agree": agreed != config.TERMS_VERSION}


def _pending(u):
    p = u.get("pending_change")
    return {"plan": p["plan"], "period": p["interval"], "at": p["at"].isoformat()} if p else None


class TermsIn(BaseModel):
    version: str


class CancelReasonIn(BaseModel):
    reason: str  # price・nouse・accuracy・other_service・other


@router.post("/me/cancel-reason")
def post_cancel_reason(body: CancelReasonIn, authorization: str = Header("")):
    """解約の手続きのあとに聞く理由（1つ）。日報に件数が載る。"""
    import growth
    uid, u = _me(authorization)
    if not growth.on_cancel_reason(uid, body.reason):
        raise HTTPException(422, {"code": "reason", "choices": growth.CANCEL_REASONS})
    return {"ok": True}


@router.put("/me/terms")
def put_terms(body: TermsIn, authorization: str = Header("")):
    uid, u = _me(authorization)
    if body.version != config.TERMS_VERSION:
        raise HTTPException(409, {"code": "terms_changed", "message": "利用規約が改められました。最新の規約をご確認ください。",
                                  "current_version": config.TERMS_VERSION})
    store.update_user(uid, {"agreed_at": store.now(), "terms_version": config.TERMS_VERSION})
    return {"ok": True}


def _referral_me(uid):
    import referral
    return referral.me(uid)


class ReferralIn(BaseModel):
    code: str


@router.post("/referral")
def post_referral(body: ReferralIn, authorization: str = Header("")):
    """紹介コードを登録する。ないコードは404、使えない（自分のコード・有料契約歴ありなど）は409。"""
    import referral
    uid, u = _me(authorization)
    code = referral.parse(body.code)
    res = referral.accept(uid, code) if code else "invalid"
    if res == "invalid":
        raise HTTPException(404, "紹介コードが見つかりません")
    if res == "not_eligible":
        raise HTTPException(409, "この紹介コードはお使いいただけません")
    return {"ok": True, "referral": referral.me(uid)}


class FaxReceiveIn(BaseModel):
    number: str


@router.put("/me/fax-receive")
def put_fax_receive(body: FaxReceiveIn, authorization: str = Header("")):
    """受け取り用FAXの送り先番号。番号のチェックは内見FAXと同じ（携帯・0120・0570・国際番号は不可）。"""
    import naiken
    uid, u = _me(authorization)
    if not naiken.fax_ok(body.number):
        raise HTTPException(422, "この番号にはFAXをお送りできません。固定電話のFAX番号をご確認ください。")
    store.update_user(uid, {"delivery": {**(u.get("delivery") or {}), "fax": naiken.fax_display(body.number)}})
    return {"ok": True, "number": naiken.fax_display(body.number)}


class CompanyIn(BaseModel):
    会社名: str
    店舗名: str = ""
    免許: str
    電話: str
    FAX: str = ""
    住所: str
    賃貸_仲介手数料_ヶ月: float = 1.0
    テーマ色: str = "青"
    書体: str = "ゴシック"
    担当者名: str = ""
    ローマ字: str = ""
    役職: str = ""
    携帯: str = ""
    携帯を載せる: bool = True  # false なら帯替え図面に携帯を載せない（名前は載る、内見申込書には載る）
    担当者メール: str = ""
    受け取り方: str = "app"
    メール: str = ""
    FAX送り先: str = ""
    宛名を聞く: bool = True  # false なら初期費用概算書の宛名を聞かない（空欄で作る）
    # 売買版（SALE_ENABLED=1 のときだけ使う。送られなければ今の値のまま）
    使い方: str | None = None          # rent / sale / both
    所属団体_種類: str | None = None    # takken / zennichi / frk / zenjukyo / other / none
    所属団体: str | None = None         # 帯の表記（種類が other のとき、または手で直したとき）
    取引態様: str | None = None         # 媒介 / 代理
    売買設定: dict | None = None        # costs_sale.CompanySaleSettings と同じ名前（fee_mode など）
    売買ローン目安: dict | None = None  # {"show": true, "rate": 1.0, "years": 35}


@router.put("/me/company")
def put_company(body: CompanyIn, authorization: str = Header("")):
    import person
    uid, u = _me(authorization)
    co = body.model_dump()
    sale = _sale_fields(co, u)
    # 担当者名（姓・名）と携帯は必須。名前の確認と変更の制限はLINE版と同じ（10/28本人決定）
    co["担当者名"] = person.normalize_name(co["担当者名"])
    mobile = person.normalize_mobile(co["携帯"])
    if not co["担当者名"]:
        raise HTTPException(422, {"code": "name_required", "message": texts.raw("reg_staff")})
    if not mobile:
        raise HTTPException(422, {"code": "mobile_check", "message": texts.raw("reg_mobile_retry")})
    co["携帯"] = mobile
    if co["担当者名"] != (u.get("company") or {}).get("担当者名") and not u.get("review"):
        until = person.change_wait(u)
        if until:
            d = until.astimezone(store.JST)
            raise HTTPException(429, {"code": "name_locked", "message": texts.raw(
                "reg_staff_locked", 変更できる日時=f"{d.month}月{d.day}日（{'月火水木金土日'[d.weekday()]}）{d.hour}時")})
        if person.try_name(uid, {**u, "company": {**(u.get("company") or {}), **{k: co[k] for k in ("会社名", "店舗名")}}},
                           co["担当者名"]) == "retry":
            raise HTTPException(422, {"code": "name_check", "message": texts.raw("reg_staff_retry")})
        u = store.get_user(uid)
        if u.get("shop_code"):
            import campaign
            campaign._set_name(u["shop_code"], uid, co["担当者名"])
    delivery = {"method": co.pop("受け取り方"), "email": co.pop("メール"), "fax": co.pop("FAX送り先")}
    co["メール"] = co.pop("担当者メール")  # 名刺に載せるメール（受け取り用とは別）
    co["帯画像"] = (u.get("company") or {}).get("帯画像")
    co["名刺済み"] = bool(co.get("担当者名"))
    co.update(sale.pop("company"))
    store.update_user(uid, {"company": co, "delivery": delivery, "registered": True, **sale})
    return {"ok": True}


def _sale_on(uid):
    import sale_bot
    return sale_bot.ENABLED and (not sale_bot.ALLOW or uid in sale_bot.ALLOW)


SALE_KEYS = ("所属団体_種類", "所属団体", "取引態様", "売買設定", "売買ローン目安")


def _sale_fields(co, u):
    """売買版の項目を取り出す。送られなかった項目は今の登録のまま残す（賃貸だけのアプリからの保存で消さない）。"""
    import sale_bot
    old = u.get("company") or {}
    kind = co.pop("使い方", None)
    out = {k: old[k] for k in SALE_KEYS if k in old}
    vals = {k: co.pop(k, None) for k in SALE_KEYS}
    if vals["所属団体_種類"] in sale_bot.ASSOC or vals["所属団体_種類"] == "none":
        out["所属団体_種類"] = vals["所属団体_種類"]
        out["所属団体"] = sale_bot.assoc_text(vals["所属団体_種類"], co.get("住所"))
    if vals["所属団体"] is not None:  # 手で直した表記が優先
        out["所属団体"] = vals["所属団体"][:80]
        out.setdefault("所属団体_種類", "other")
    if vals["取引態様"] in ("媒介", "代理"):
        out["取引態様"] = vals["取引態様"]
    if vals["売買設定"] is not None:
        ok = {k: v for k, v in vals["売買設定"].items() if k in sale_bot.DEFAULT_SETTINGS}
        if ok.get("fee_mode") not in (None, "max", "half", "free", "custom"):
            raise HTTPException(422, {"code": "sale_fee", "message": "売買の仲介手数料の選び方が正しくありません。"})
        out["売買設定"] = {**(old.get("売買設定") or {}), **ok}
    if vals["売買ローン目安"] is not None:
        out["売買ローン目安"] = {**sale_bot.DEFAULT_LOAN, **{k: v for k, v in vals["売買ローン目安"].items()
                                                         if k in sale_bot.DEFAULT_LOAN}}
    res = {"company": out}
    if kind in ("rent", "sale", "both"):
        res["use_kind"] = kind
    return res


def _run_sale_job(uid, u, jid, data, rental_a):
    """アプリから届いた売買の図面（賃貸版の読み取りで「賃貸以外」）。売買版で読み直して作る。"""
    import sale_bot
    import sale_core
    if not sale_core.route_from_rental(rental_a["result"]):
        return store.update_job(jid, {"status": "failed", "error": "not_maisoku"})
    a = sale_core.analyze(data)
    v = sale_core.verdict(a["result"])
    if v != "ok":
        err = f"sale_unsupported:{a['result'].事実.物件種別 or ''}" if v == "unsupported" else f"sale_{v}"
        return store.update_job(jid, {"status": "failed", "error": err, "log": a["log"]})
    if sale_bot.needs_setup(u):
        # アプリでは「売買の設定」画面を出してもらう（PUT /v1/me/company のあと POST /v1/jobs/{id}/resume）
        sale_bot._save_analyzed(jid, a)
        return store.update_job(jid, {"status": "needs_sale_setup"})
    _finish_sale(uid, u, jid, a)


def _finish_sale(uid, u, jid, a):
    import sale_bot
    import sale_core
    if not store.try_consume(uid):
        return store.update_job(jid, {"status": "failed", "error": "quota"})
    try:
        atena = (store.get_job(jid) or {}).get("宛名") or ""
        company = {**bot.company_for_render(uid, u), "宛名": atena}
        out = sale_core.render_documents(company, a["img"], a["result"], a["pages"], a["log"],
                                         estimate_html=sale_bot.estimate_html(company))
        store.save_files(jid, out)
        store.update_job(jid, {"status": "done", "kind": "sale", "title": out["title"], "texts": out["texts"],
                               "log": out["log"], "pages": out["pages"], "verdict": "sale", "result": out["result"]})
    except Exception:
        store.refund(uid)
        raise


@router.post("/jobs/{jid}/resume")
def resume_job(jid: str, authorization: str = Header("")):
    """売買の設定（所属団体・売買の仲介手数料）を登録したあと、預かった売買の図面で作る。AIはもう一度呼ばない。"""
    import sale_bot
    uid, u = _me(authorization)
    job = store.get_job(jid)
    if not job or job["user_id"] != uid:
        raise HTTPException(404)
    if job.get("status") != "needs_sale_setup":
        raise HTTPException(409, "この資料は作り直せません")
    if sale_bot.needs_setup(u):
        raise HTTPException(409, {"code": "needs_sale_setup", "message": "所属団体と売買の仲介手数料を登録してください。"})
    store.update_job(jid, {"status": "processing"})
    bot.pool.submit(_safe_resume, uid, u, jid, job)
    return {"job_id": jid}


def _safe_resume(uid, u, jid, job):
    import sale_bot
    try:
        _finish_sale(uid, u, jid, sale_bot._load_analyzed(jid, job))
    except Exception:
        log.exception("アプリの売買の資料づくりで例外")
        store.update_job(jid, {"status": "failed", "error": "system"})


def _run_job(uid, jid, data):
    u = store.get_user(uid)
    consumed = False
    try:
        store.update_job(jid, {"status": "processing"})
        a = core.analyze(data)
        v = core.verdict(a["result"])
        import sale_bot
        if v == "sale" and _sale_on(uid):
            return _run_sale_job(uid, u, jid, data, a)
        if v not in ("ok", "commercial"):
            store.update_job(jid, {"status": "failed", "error": v, "log": a["log"]})
            return
        if not store.try_consume(uid):
            store.update_job(jid, {"status": "failed", "error": "quota"})
            return
        consumed = True
        # 読み取り中に宛名が届いていれば、初期費用概算書に入れる（PUT /v1/jobs/{id}/atena）
        atena = (store.get_job(jid) or {}).get("宛名") or ""
        out = core.render_documents({**bot.company_for_render(uid, u), "宛名": atena},
                                    a["img"], a["result"], a["pages"], a["log"])
        store.save_files(jid, out)
        store.update_job(jid, {"status": "done", "title": out["title"], "texts": out["texts"], "log": out["log"],
                               "pages": out["pages"], "verdict": v, "result": out["result"]})
        latest = (store.get_job(jid) or {}).get("宛名") or ""
        if latest != atena:  # 作っている間に宛名が届いたとき
            bot.rerender_atena(uid, u, jid, latest)
        # 受け取り用FAXは自動では送らない。帯替え図面を見てから POST /v1/jobs/{id}/fax-receive（10/28本人決定）
    except core.InputError as e:
        if consumed:
            store.refund(uid)
        store.update_job(jid, {"status": "failed", "error": str(e)})
    except Exception as e:
        log.exception("アプリの資料づくりで例外")
        if consumed:
            store.refund(uid)
        store.update_job(jid, {"status": "failed", "error": "system"})


@router.post("/jobs")
async def create_job(file: UploadFile = File(...), authorization: str = Header("")):
    uid, u = _me(authorization)
    if not u.get("registered"):
        raise HTTPException(409, "先に会社情報を登録してください")
    q = store.quota(u)
    if q["remaining"] is not None and q["remaining"] <= 0:
        raise HTTPException(402, "今月の件数を使い切りました")
    import usage
    if usage.ai_stopped():
        raise HTTPException(503, {"code": "busy", "message": "ただいま大変混み合っているため、資料づくりを一時お休みしています。"})
    if usage.over_daily(u):
        raise HTTPException(429, {"code": "daily_limit", "message": f"本日の資料づくりは、上限の{usage.DAILY_DOC_LIMIT}件に達しました。明日0時から、またお使いいただけます。"})
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "ファイルが大きすぎます（20MBまで）")
    jid, job = store.new_job(uid, "app")
    bot.pool.submit(_run_job, uid, jid, data)
    return {"job_id": jid}


@router.get("/jobs/{jid}")
def get_job(jid: str, authorization: str = Header("")):
    uid, u = _me(authorization)
    job = store.get_job(jid)
    if not job or job["user_id"] != uid:
        raise HTTPException(404)
    ca = job.get("created_at")
    res = {"job_id": jid, "created_at": ca.isoformat() if hasattr(ca, "isoformat") else ca,
           "status": job["status"], "error": job.get("error"), "title": job.get("title"),
           "kind": job.get("kind") or "rent",  # 売買版：rent / sale（アプリは sale のとき売買の画面を出す）
           "pdf_url": None, "obikae_url": None, "texts": None}
    if job["status"] == "done":
        mz = (job.get("texts") or {}).get("motozuke") or {}
        res.update(motozuke=mz.get("name"), motozuke_fax=mz.get("fax"), motozuke_tel=mz.get("tel"))
        # 賃料などが区画・使い方で2通り以上書いてある図面。choice が null なら PUT /v1/jobs/{id}/choice で選んでもらう
        f = (job.get("result") or {}).get("事実") or {}
        res.update(choices=[c["名前"] for c in (f.get("条件が複数") or [])], choice=f.get("選んだ条件"))
        okyaku, shanai = store.pdf_urls(jid, job)
        res.update(pdf_url=store.file_url(jid, job, "shiryou.pdf"), customer_pdf_url=okyaku, staff_pdf_url=shanai,
                   obikae_url=store.file_url(jid, job, "obikae.png"),
                   texts=job.get("texts"), expires_at=job["expires_at"].isoformat())
    res.update(fax_receive=job.get("fax_receive"), fax_tickets_left=faxopt.left(u))
    return res


@router.post("/jobs/{jid}/fax-receive")
def job_fax_receive(jid: str, authorization: str = Header("")):
    """「自社のFAXに送信する（1回分）」「もう一度送る」。回数券を1回使って2枚を送り、更新後の Job を返す。"""
    uid, u = _me(authorization)
    job = store.get_job(jid)
    if not job or job["user_id"] != uid or job["status"] != "done":
        raise HTTPException(404)
    if not (u.get("delivery") or {}).get("fax"):
        raise HTTPException(402, "受け取り用のFAX番号が登録されていません")
    if not delivery.fax_ready():
        raise HTTPException(503, "ただいまFAXでのお届けを準備中です")
    # お試し中は最初の3件まで無料、そのあとは回数券（bot.recvfax_send と同じ）
    res, n = bot.recvfax_send(uid, u, jid, job)
    if res == "trial_over":
        raise HTTPException(402, {"code": "trial_over", "message": texts.raw("recvfax_trial_over")})
    if res == "empty":
        raise HTTPException(402, {"code": "empty", "message": "受け取り用FAXの回数券がありません"})
    if res != "sent":
        raise HTTPException(502, "FAXを送れませんでした。時間をおいてもう一度お試しください")
    store.update_job(jid, {"fax_receive": "sent", "fax_tickets_left": n})
    return get_job(jid, authorization)


class AtenaIn(BaseModel):
    atena: str = ""


@router.put("/jobs/{jid}/atena")
def job_atena(jid: str, body: AtenaIn, authorization: str = Header("")):
    """初期費用概算書の宛名（"" なら空欄）。読み取り中なら資料を作るときに使い、
    できあがっていれば宛名だけ差し替えて作り直す（件数に数えない）。更新後の Job を返す。"""
    uid, u = _me(authorization)
    job = store.get_job(jid)
    if not job or job["user_id"] != uid:
        raise HTTPException(404)
    atena = bot.parse_atena(body.atena)
    if job["status"] == "done":
        if not bot.rerender_atena(uid, u, jid, atena):
            raise HTTPException(410, "資料の保存期間が過ぎたため、宛名を変えられませんでした")
    elif job["status"] == "failed":
        raise HTTPException(409, "この資料は作れませんでした")
    else:
        store.update_job(jid, {"宛名": atena})
        # 書き込みの直前に資料ができあがっていた場合に備えて、もう一度確かめる
        if (store.get_job(jid) or {}).get("status") == "done":
            bot.rerender_atena(uid, u, jid, atena)
    res = get_job(jid, authorization)
    res["atena"] = atena
    return res


class ChoiceIn(BaseModel):
    index: int


@router.put("/jobs/{jid}/choice")
def job_choice(jid: str, body: ChoiceIn, authorization: str = Header("")):
    """条件が2通り以上ある図面で、どれで初期費用の概算書を作るか（choices の番号、0から）。作り直して更新後の Job を返す。"""
    uid, u = _me(authorization)
    job = store.get_job(jid)
    if not job or job["user_id"] != uid:
        raise HTTPException(404)
    if job["status"] != "done":
        raise HTTPException(409, "資料ができてから選んでください")
    if not bot.apply_choice(uid, u, jid, body.index):
        raise HTTPException(410, "資料の保存期間が過ぎたか、選べない番号です")
    return get_job(jid, authorization)


class FixIn(BaseModel):
    text: str


@router.post("/jobs/{jid}/fix")
def job_fix(jid: str, body: FixIn, authorization: str = Header("")):
    """「ここを消して」の直し。頼まれた文字を白で消して作り直し、更新後の Job を返す（件数に数えない）。
    消せなかったときは fix.status が "none" で、fix.note に利用者へそのまま見せる説明が入る。"""
    uid, u = _me(authorization)
    job = store.get_job(jid)
    if not job or job["user_id"] != uid:
        raise HTTPException(404)
    if not body.text.strip():
        raise HTTPException(422, "消したいところを入力してください")
    r = bot.do_fix(uid, u, jid, body.text.strip())
    if r["status"] in ("missing", "expired"):
        raise HTTPException(410, "直せる資料が見つかりませんでした。募集図面をもう一度送ってください")
    res = get_job(jid, authorization)
    res["fix"] = r
    return res


class PlayIn(BaseModel):
    productId: str
    purchaseToken: str


@router.post("/billing/google/verify")
def google_verify(body: PlayIn, authorization: str = Header("")):
    import play
    uid, u = _me(authorization)
    res = play.verify(uid, body.productId, body.purchaseToken)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "確認できませんでした"))
    return res


@router.post("/billing/google/rtdn")
async def google_rtdn(request: Request, token: str = ""):
    """Pub/Sub の push。URL に ?token= を付けて登録し、合わないものは捨てる。"""
    import hmac as _hmac
    import play
    if not _hmac.compare_digest(token, config.env("PUBSUB_TOKEN") or config.APP_SECRET):
        raise HTTPException(403)
    play.rtdn(await request.json())
    return {"ok": True}


class AppleIn(BaseModel):
    productId: str
    signedTransaction: str  # StoreKit 2 の jwsRepresentation


@router.post("/billing/apple/verify")
def apple_verify(body: AppleIn, authorization: str = Header("")):
    import appstore
    uid, u = _me(authorization)
    res = appstore.verify(uid, body.productId, body.signedTransaction)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "確認できませんでした"))
    return res


@router.post("/billing/apple/notify")
async def apple_notify(request: Request):
    """App Store Server Notifications V2。中身は Apple の署名で確かめるので、URLの合言葉は不要。"""
    import appstore
    appstore.notify(await request.json())
    return {"ok": True}


# ---------- 内見申込書FAX ----------

class NaikenIn(BaseModel):
    job_id: str | None = None
    to_company: str
    to_branch: str = ""
    to_fax: str
    prop_name: str
    prop_room: str | None = None
    prop_address: str | None = None
    # 表示する文字のまま（例「10月30日（金） 14:00」、日付だけ・時間だけも可）か "YYYY-MM-DD HH:MM"。
    # 空文字は空欄の行（順番を保つ）。全部空なら []
    wishes: list[str] = []
    guests: int | None = None
    guide: str | None = None


def _wish(s):
    import re as _re
    m = _re.match(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?$", s.strip())
    return {"date": m.group(1), "time": m.group(2)} if m else {"text": s.strip()}


@router.post("/naiken/preview")
def naiken_preview(body: NaikenIn, authorization: str = Header("")):
    import naiken
    uid, u = _me(authorization)
    if not naiken.fax_ok(body.to_fax):
        raise HTTPException(422, "このFAX番号にはお送りできません（携帯・0120・0570・国際番号などは不可）")
    d = {"job_id": body.job_id, "to": {"company": body.to_company, "branch": body.to_branch, "fax": body.to_fax},
         "prop": {"name": body.prop_name, "room": body.prop_room, "address": body.prop_address or ""},
         "wishes": [_wish(w) for w in body.wishes[:2]], "guests": body.guests, "guide": body.guide}
    fid, rec = naiken.save_preview(uid, d, u.get("company") or {})
    return {"fax_id": fid, "status": "preview", "preview_url": naiken.file_url(fid, rec, "naiken.png"),
            "pdf_url": naiken.file_url(fid, rec, "naiken.pdf")}


@router.post("/naiken/{fid}/send")
def naiken_send(fid: str, authorization: str = Header("")):
    import naiken
    uid, u = _me(authorization)
    status = naiken.send(uid, fid)
    if status is None:
        raise HTTPException(404)
    if status == "trial":
        # 無料お試し中は内見申込書FAXを使えない（有料プランの機能）
        raise HTTPException(403, {"code": "trial", "message": texts.raw("naiken_trial")})
    if status == "limit":
        raise HTTPException(402, "内見FAXの回数の上限に達しました")
    if status == "optout":
        # 元付業者さんの依頼で送信を止めている番号（回数には数えない）
        raise HTTPException(409, {"code": "optout", "message": "この管理会社さまからFAXを止めるご依頼をいただいているため、お送りできません。お電話など直接ご連絡をお願いいたします。"})
    return {"status": status}


class ReserveIn(BaseModel):
    send_at: str  # 日本時間の「YYYY-MM-DDTHH:MM」


def _reserve_fail(status, fid):
    import naiken
    if status is None:
        raise HTTPException(404)
    if status == "trial":
        raise HTTPException(403, {"code": "trial", "message": texts.raw("naiken_trial")})
    if status == "limit":
        raise HTTPException(402, "内見FAXの回数の上限に達しました")
    if status == "optout":
        raise HTTPException(409, {"code": "optout", "message": "この管理会社さまからFAXを止めるご依頼をいただいているため、お送りできません。お電話など直接ご連絡をお願いいたします。"})
    if status in naiken.RESERVE_ERRORS:
        rec = store.db().get("faxes", fid) or {}
        raise HTTPException(422, {"code": status, "message": naiken.error_text(status, rec.get("draft"))})


def _reserved_out(fid, rec):
    import naiken
    return {"fax_id": fid, "status": rec["status"], "send_at": rec["send_at"].astimezone(store.JST).isoformat(),
            "send_at_label": naiken.when_label(rec["send_at"]), "holiday_note": naiken.holiday_note(rec["send_at"]),
            "to_company": (rec["draft"].get("to") or {}).get("company", ""),
            "prop_name": (rec["draft"].get("prop") or {}).get("name", "")}


@router.get("/naiken/reserved")
def naiken_reserved(authorization: str = Header("")):
    """予約中の内見FAX（送信の早い順）。"""
    import naiken
    uid, u = _me(authorization)
    return {"items": [_reserved_out(fid, rec) for fid, rec in naiken.reserved(uid)]}


@router.post("/naiken/{fid}/reserve")
def naiken_reserve(fid: str, body: ReserveIn, authorization: str = Header("")):
    """日時を指定して送る（8時〜20時・15分後〜7日後・内見の第1希望の1時間前まで）。回数は予約した時点で1回分使う。"""
    import naiken
    uid, u = _me(authorization)
    status = naiken.reserve(uid, fid, naiken.parse_when(body.send_at))
    if status != "scheduled":
        _reserve_fail(status, fid)
        return {"status": status}
    return _reserved_out(fid, store.db().get("faxes", fid))


@router.put("/naiken/{fid}/reserve")
def naiken_reschedule(fid: str, body: ReserveIn, authorization: str = Header("")):
    """予約の日時を変える（送信の5分前まで）。"""
    import naiken
    uid, u = _me(authorization)
    status = naiken.reschedule(uid, fid, naiken.parse_when(body.send_at))
    if status != "scheduled":
        _reserve_fail(status, fid)
    return _reserved_out(fid, store.db().get("faxes", fid))


@router.delete("/naiken/{fid}/reserve")
def naiken_unreserve(fid: str, authorization: str = Header("")):
    """予約を取り消す（送信の5分前まで）。押さえた回数は戻る。"""
    import naiken
    uid, u = _me(authorization)
    status = naiken.unreserve(uid, fid)
    if status != "canceled":
        _reserve_fail(status, fid)
    return {"status": "canceled"}


@router.get("/naiken/{fid}")
def naiken_get(fid: str, authorization: str = Header("")):
    import naiken
    uid, u = _me(authorization)
    rec = store.db().get("faxes", fid)
    if not rec or rec["user_id"] != uid:
        raise HTTPException(404)
    return {"status": rec["status"], "pdf_url": naiken.file_url(fid, rec, "naiken.pdf"), "error": rec.get("error"),
            "send_at": rec["send_at"].astimezone(store.JST).isoformat() if rec.get("send_at") else None}
