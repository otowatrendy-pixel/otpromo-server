"""アプリ版（Android）の REST API。ログインは Firebase Authentication の IDトークン。

POST /v1/jobs            画像かPDF（multipart の file）→ {job_id}
GET  /v1/jobs/{id}       → {status, pdf_url, obikae_url, texts, error}
GET  /v1/me              → {plan, plan_source, remaining, free_left, registered, company}
PUT  /v1/me/company      会社情報（帯・色・書体・受け取り方）
POST /v1/billing/google/verify, /v1/billing/google/rtdn  … Google Play の課金（あとで実装）
"""
import logging

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from pydantic import BaseModel

import bot
import config
import core
import store

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
    q = store.quota(u)
    return {"plan": q["plan"], "plan_source": u.get("plan_source"), "remaining": q["remaining"],
            "free_left": q["free_left"], "registered": u.get("registered", False),
            "company": u.get("company") or {}, "delivery": u.get("delivery") or {}}


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
    受け取り方: str = "app"
    メール: str = ""
    FAX送り先: str = ""


@router.put("/me/company")
def put_company(body: CompanyIn, authorization: str = Header("")):
    uid, u = _me(authorization)
    co = body.model_dump()
    delivery = {"method": co.pop("受け取り方"), "email": co.pop("メール"), "fax": co.pop("FAX送り先")}
    co["帯画像"] = (u.get("company") or {}).get("帯画像")
    store.update_user(uid, {"company": co, "delivery": delivery, "registered": True})
    return {"ok": True}


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
                               "pages": out["pages"], "verdict": v})
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
        raise HTTPException(402, "今月の枚数を使い切りました")
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
        res.update(pdf_url=store.file_url(jid, job, "shiryou.pdf"), obikae_url=store.file_url(jid, job, "obikae.png"),
                   texts=job.get("texts"), expires_at=job["expires_at"].isoformat())
    return res


@router.post("/billing/google/verify")
def google_verify():
    raise HTTPException(501, "Google Play の課金はまだ準備中です")


@router.post("/billing/google/rtdn")
def google_rtdn():
    return {"ok": True}
