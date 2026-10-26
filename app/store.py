"""利用者・作成履歴（Firestore）とファイル（Cloud Storage）。

STORE=local のときはメモリとローカルフォルダで代用する（手元の確認用）。

users/{id}
  line_uid, firebase_uid       … 入口ごとの識別子（どちらか、または両方）
  registered                   … 初回登録が済んだか
  company{...}                 … 帯・色・書体など（core.DEFAULT_COMPANY と同じ項目）
  delivery{method, email, fax} … 受け取り方（line / email / fax）
  plan                         … free / light / unlimited
  plan_source                  … stripe / google_play
  fax_option                   … FAXオプションの有無
  stripe_customer, stripe_subscription
  free_limit                   … お試しの枚数（既定5。合言葉などで増やせる）
  free_used                    … お試しで使った枚数（通算）
  month_key, month_used        … 今月（日本時間）の枚数
  promo_codes[]                … 使った合言葉
  referral_code, referred_by   … 紹介（後で使う）
jobs/{id}
  user_id, source(line/app), status(queued/processing/done/failed), title, texts, error, log,
  token（ファイルのリンク用）, expires_at, created_at
"""
import copy
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

JST = timezone(timedelta(hours=9))


def now():
    return datetime.now(timezone.utc)


def month_key():
    return datetime.now(JST).strftime("%Y-%m")


def new_user(**ids):
    return {
        "line_uid": None, "firebase_uid": None, "registered": False, "company": {},
        "delivery": {"method": "line", "email": "", "fax": ""},
        "plan": "free", "plan_source": None, "fax_option": False,
        "stripe_customer": None, "stripe_subscription": None,
        "free_limit": config.FREE_SHEETS, "free_used": 0, "month_key": month_key(), "month_used": 0,
        "promo_codes": [], "referral_code": None, "referred_by": None,
        "created_at": now(), **ids,
    }


# ---------- 保存先の実装 ----------

class _Local:
    def __init__(self):
        self.docs = {"users": {}, "jobs": {}, "events": {}, "faxes": {}, "meta": {}, "play_tokens": {}}
        self.lock = threading.Lock()
        self.dir = Path(config.LOCAL_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)

    def get(self, col, id):
        d = self.docs[col].get(id)
        return copy.deepcopy(d) if d else None

    def set(self, col, id, data, merge=False):
        with self.lock:
            if merge and id in self.docs[col]:
                self.docs[col][id].update(copy.deepcopy(data))
            else:
                self.docs[col][id] = copy.deepcopy(data)

    def create(self, col, id, data):
        """すでにあれば False（Webhookの二重受信よけ）。"""
        with self.lock:
            if id in self.docs[col]:
                return False
            self.docs[col][id] = copy.deepcopy(data)
            return True

    def find(self, col, field, value):
        for k, v in self.docs[col].items():
            if v.get(field) == value:
                return k, copy.deepcopy(v)
        return None, None

    def update_tx(self, col, id, fn):
        """取り出し→fnで変更→保存を一度に行う。fn の戻り値をそのまま返す。"""
        with self.lock:
            d = self.docs[col].get(id)
            out = fn(d)
            self.docs[col][id] = d
            return out

    def put_file(self, key, data, ctype):
        p = self.dir / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get_file(self, key):
        p = self.dir / key
        return p.read_bytes() if p.exists() else None


class _GCP:
    def __init__(self):
        from google.cloud import firestore, storage
        self.fs = firestore.Client(project=config.GCP_PROJECT)
        self.firestore = firestore
        self.bucket = storage.Client(project=config.GCP_PROJECT).bucket(config.BUCKET)

    def get(self, col, id):
        s = self.fs.collection(col).document(id).get()
        return s.to_dict() if s.exists else None

    def set(self, col, id, data, merge=False):
        self.fs.collection(col).document(id).set(data, merge=merge)

    def create(self, col, id, data):
        from google.api_core.exceptions import AlreadyExists
        try:
            self.fs.collection(col).document(id).create(data)
            return True
        except AlreadyExists:
            return False

    def find(self, col, field, value):
        from google.cloud.firestore_v1.base_query import FieldFilter
        for s in self.fs.collection(col).where(filter=FieldFilter(field, "==", value)).limit(1).stream():
            return s.id, s.to_dict()
        return None, None

    def update_tx(self, col, id, fn):
        ref = self.fs.collection(col).document(id)
        tx = self.fs.transaction()

        @self.firestore.transactional
        def run(tx):
            d = ref.get(transaction=tx).to_dict()
            out = fn(d)
            tx.set(ref, d)
            return out
        return run(tx)

    def put_file(self, key, data, ctype):
        self.bucket.blob(key).upload_from_string(data, content_type=ctype)

    def get_file(self, key):
        b = self.bucket.blob(key)
        try:
            return b.download_as_bytes()
        except Exception:
            return None


_db = None


def db():
    global _db
    if _db is None:
        _db = _Local() if config.STORE == "local" else _GCP()
    return _db


# ---------- 利用者 ----------

def user_by_line(line_uid, create=True):
    uid = "L" + line_uid
    u = db().get("users", uid)
    if u is None and create:
        u = new_user(line_uid=line_uid)
        db().create("users", uid, u)
        u = db().get("users", uid)
    return uid, u


def user_by_firebase(fb_uid, create=True):
    uid, u = db().find("users", "firebase_uid", fb_uid)
    if u is None and create:
        uid = "F" + fb_uid
        u = new_user(firebase_uid=fb_uid)
        db().create("users", uid, u)
        u = db().get("users", uid)
    return uid, u


def get_user(uid):
    return db().get("users", uid)


def update_user(uid, data):
    db().set("users", uid, data, merge=True)


# ---------- 枚数 ----------

def period_key(u):
    """枚数を数える区切り。有料プランは請求期間の始まり、それ以外は日本時間の暦の月。"""
    ps = u.get("period_start")
    if (u.get("plan") or "free") != "free" and ps:
        return "P" + ps.astimezone(JST).strftime("%Y-%m-%d")
    return month_key()


def renew_date(u):
    """次に枚数が戻る日（日本時間、表示用）。"""
    pe = u.get("period_end")
    if (u.get("plan") or "free") != "free" and pe:
        d = pe.astimezone(JST)
    else:
        n = datetime.now(JST)
        d = datetime(n.year + (n.month == 12), n.month % 12 + 1, 1, tzinfo=JST)
    return f"{d.month}月{d.day}日"


# ライトのAI資料は「付与」単位で持つ：毎期20枚を付与、付与から3か月で失効、残高は最大40枚（繰り越し可）
GRANT_DAYS = int(config.env("AI_GRANT_DAYS", "90"))
AI_CAP = int(config.env("AI_BALANCE_CAP", "40"))
UNLIMITED_SOFT_CAP = int(config.env("UNLIMITED_SOFT_CAP", "300"))  # 規約上の目安。超えたらログに残す


def _live_grants(u):
    t = now()
    return [g for g in (u.get("ai_grants") or []) if g["exp"] > t and g["used"] < g["n"]]


def ai_balance(u):
    return sum(g["n"] - g["used"] for g in _live_grants(u))


def _roll_month(u):
    if u.get("month_key") == period_key(u):
        return
    u["month_key"], u["month_used"] = period_key(u), 0
    if (u.get("plan") or "free") == "light":
        import monitor
        grant = config.LIGHT_SHEETS + monitor.bonus_sheets(u)
        room = max(0, AI_CAP + monitor.bonus_sheets(u) - ai_balance(u))
        n = min(grant, room)
        live = _live_grants(u)
        if n > 0:
            live.append({"n": n, "used": 0, "at": now(), "exp": now() + timedelta(days=GRANT_DAYS)})
        u["ai_grants"] = live


def quota(u):
    """今使える枚数の状況。remaining が None なら無制限。"""
    u = dict(u)
    _roll_month(u)
    plan = u.get("plan") or "free"
    if plan == "unlimited":
        return {"plan": plan, "remaining": None, "free_left": 0, "month_used": u["month_used"]}
    if plan == "light":
        live = sorted(_live_grants(u), key=lambda g: g["exp"])
        return {"plan": plan, "remaining": ai_balance(u), "free_left": 0, "month_used": u["month_used"],
                "next_expiry": live[0]["exp"] if live else None}
    left = max(0, u.get("free_limit", config.FREE_SHEETS) - u.get("free_used", 0))
    return {"plan": "free", "remaining": left, "free_left": left, "month_used": u["month_used"]}


def try_consume(uid):
    """1枚分を使う。使えなければ False。資料作成に失敗したら refund() で戻す。"""
    def fn(u):
        _roll_month(u)
        q = quota(u)
        if q["remaining"] is not None and q["remaining"] <= 0:
            return False
        if q["plan"] == "light":
            g = sorted(_live_grants(u), key=lambda g: g["exp"])[0]  # 期限の近い分から使う
            g["used"] += 1
        u["month_used"] = u.get("month_used", 0) + 1
        u["total_docs"] = u.get("total_docs", 0) + 1
        if q["plan"] == "free":
            u["free_used"] = u.get("free_used", 0) + 1
        if q["plan"] == "unlimited" and u["month_used"] == UNLIMITED_SOFT_CAP:
            import logging
            logging.getLogger("store").warning("使い放題の目安 %s 枚に達しました uid=%s", UNLIMITED_SOFT_CAP, uid)
        return True
    return db().update_tx("users", uid, fn)


def refund(uid):
    def fn(u):
        _roll_month(u)
        u["month_used"] = max(0, u.get("month_used", 0) - 1)
        u["total_docs"] = max(0, u.get("total_docs", 0) - 1)
        plan = u.get("plan") or "free"
        if plan == "free":
            u["free_used"] = max(0, u.get("free_used", 0) - 1)
        if plan == "light":
            used = [g for g in (u.get("ai_grants") or []) if g["used"] > 0]
            if used:
                sorted(used, key=lambda g: g["exp"])[0]["used"] -= 1
    db().update_tx("users", uid, fn)


def apply_promo(uid, code, extra):
    """合言葉でお試し枚数を増やす。1人1回。戻り値：True=適用、False=使用済み。"""
    def fn(u):
        if u.get("promo_codes") and code in u["promo_codes"]:
            return False
        if u.get("promo_codes"):  # 合言葉は1人1つまで
            return False
        u["promo_codes"] = (u.get("promo_codes") or []) + [code]
        u["free_limit"] = config.FREE_SHEETS + extra
        return True
    return db().update_tx("users", uid, fn)


# ---------- 作成履歴とファイル ----------

def new_job(uid, source):
    jid = now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
    job = {"user_id": uid, "source": source, "status": "queued", "title": None, "texts": None,
           "error": None, "log": None, "token": secrets.token_urlsafe(24),
           "expires_at": now() + timedelta(days=config.FILE_DAYS), "created_at": now()}
    db().set("jobs", jid, job)
    return jid, job


def update_job(jid, data):
    db().set("jobs", jid, data, merge=True)


def get_job(jid):
    return db().get("jobs", jid)


FILES = {"shiryou.pdf": "application/pdf", "obikae.png": "image/png",
         "obikae.jpg": "image/jpeg", "preview.jpg": "image/jpeg"}


def save_files(jid, out):
    db().put_file(f"jobs/{jid}/shiryou.pdf", out["pdf"], FILES["shiryou.pdf"])
    db().put_file(f"jobs/{jid}/obikae.png", out["obikae_png"], FILES["obikae.png"])
    db().put_file(f"jobs/{jid}/obikae.jpg", out["obikae_jpg"], FILES["obikae.jpg"])
    db().put_file(f"jobs/{jid}/preview.jpg", out["obikae_preview"], FILES["preview.jpg"])


def file_url(jid, job, name):
    return f"{config.BASE_URL}/f/{jid}/{job['token']}/{name}"


def read_file(jid, token, name):
    """リンクの合言葉と期限を確かめてからファイルを返す。"""
    if name not in FILES:
        return None
    job = get_job(jid)
    if not job or not secrets.compare_digest(job.get("token", ""), token):
        return None
    exp = job.get("expires_at")
    if exp and exp < now():
        return "expired"
    return db().get_file(f"jobs/{jid}/{name}")


def first_event(event_id):
    """Webhookの同じイベントを二度処理しないための記録。初めてなら True。"""
    return db().create("events", event_id, {"at": now()})
