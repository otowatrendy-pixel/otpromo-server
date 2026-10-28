"""アプリ版（Android）の REST API。ログインは Firebase Authentication の IDトークン。

POST /v1/jobs            画像かPDF（multipart の file）→ {job_id}
GET  /v1/jobs/{id}       → {status, pdf_url, obikae_url, texts, error}
GET  /v1/me              → {plan, plan_source, remaining, free_left, registered, company}
PUT  /v1/me/company      会社情報（帯・色・書体・受け取り方）。担当者名・携帯は必須（422 name_required/mobile_check/name_check、429 name_locked）
POST /v1/billing/google/verify, /v1/billing/google/rtdn  … Google Play の課金（あとで実装）
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


def _firebase_uid(authorization: str) -> str:
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
    return info["sub"]


def _me(authorization):
    return store.user_by_firebase(_firebase_uid(authorization))


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
            "company": u.get("company") or {}, "delivery": u.get("delivery") or {},
            # 受け取り用FAX（回数券 10回1,000円・購入から3か月）。next_expiry は一番近い期限
            # 番号が未登録なら null
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
    担当者メール: str = ""
    受け取り方: str = "app"
    メール: str = ""
    FAX送り先: str = ""


@router.put("/me/company")
def put_company(body: CompanyIn, authorization: str = Header("")):
    import person
    uid, u = _me(authorization)
    co = body.model_dump()
    # 担当者名（姓・名）と携帯は必須。名前の確認と変更の制限はLINE版と同じ（10/28本人決定）
    co["担当者名"] = person.normalize_name(co["担当者名"])
    mobile = person.normalize_mobile(co["携帯"])
    if not co["担当者名"]:
        raise HTTPException(422, {"code": "name_required", "message": texts.raw("reg_staff")})
    if not mobile:
        raise HTTPException(422, {"code": "mobile_check", "message": texts.raw("reg_mobile_retry")})
    co["携帯"] = mobile
    if co["担当者名"] != (u.get("company") or {}).get("担当者名"):
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
    store.update_user(uid, {"company": co, "delivery": delivery, "registered": True})
    return {"ok": True}


def _fax_receive(uid, jid):
    """受け取り用FAXの番号がある方には、2枚（帯替え図面＋概算書）をFAXでも送る（回数券を1回使う）。
    job の fax_receive は "sent"・"empty"（回数券なし）。"""
    u = store.get_user(uid)
    if not (u.get("delivery") or {}).get("fax"):
        return
    try:
        res, n = bot.recvfax_send(uid, u, jid, store.get_job(jid))
        if res:
            store.update_job(jid, {"fax_receive": res, "fax_tickets_left": n})
    except Exception:
        log.exception("受け取り用FAXの送信で例外 uid=%s", uid)


def _run_job(uid, jid, data):
    u = store.get_user(uid)
    consumed = False
    try:
        store.update_job(jid, {"status": "processing"})
        a = core.analyze(data)
        v = core.verdict(a["result"])
        if v not in ("ok", "commercial"):
            store.update_job(jid, {"status": "failed", "error": v, "log": a["log"]})
            return
        if not store.try_consume(uid):
            store.update_job(jid, {"status": "failed", "error": "quota"})
            return
        consumed = True
        out = core.render_documents(bot.company_for_render(uid, u), a["img"], a["result"], a["pages"], a["log"])
        store.save_files(jid, out)
        store.update_job(jid, {"status": "done", "title": out["title"], "texts": out["texts"], "log": out["log"],
                               "pages": out["pages"], "verdict": v, "result": out["result"]})
        _fax_receive(uid, jid)
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
    res = {"status": job["status"], "error": job.get("error"), "title": job.get("title"),
           "pdf_url": None, "obikae_url": None, "texts": None}
    if job["status"] == "done":
        mz = (job.get("texts") or {}).get("motozuke") or {}
        res.update(motozuke=mz.get("name"), motozuke_fax=mz.get("fax"), motozuke_tel=mz.get("tel"))
        res.update(pdf_url=store.file_url(jid, job, "shiryou.pdf"), obikae_url=store.file_url(jid, job, "obikae.png"),
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
    if not faxopt.can_send(u):
        raise HTTPException(402, "受け取り用FAXの回数券がありません")
    if not delivery.fax_ready():
        raise HTTPException(503, "ただいまFAXでのお届けを準備中です")
    n = faxopt.send_job(uid, u, jid, job, bot.company_for_render(uid, u))
    if n is None:
        raise HTTPException(502, "FAXを送れませんでした。時間をおいてもう一度お試しください")
    store.update_job(jid, {"fax_receive": "sent", "fax_tickets_left": n})
    return get_job(jid, authorization)


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
    return {"status": status}


@router.get("/naiken/{fid}")
def naiken_get(fid: str, authorization: str = Header("")):
    import naiken
    uid, u = _me(authorization)
    rec = store.db().get("faxes", fid)
    if not rec or rec["user_id"] != uid:
        raise HTTPException(404)
    return {"status": rec["status"], "pdf_url": naiken.file_url(fid, rec, "naiken.pdf"), "error": rec.get("error")}
