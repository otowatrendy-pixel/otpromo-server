"""担当者名と携帯（アカウントの使い回し・貸与を防ぐ、10/28本人決定）。

- 登録で担当者名（姓・名のフルネーム）と携帯番号を必須にし、帯替え図面と内見申込書に「担当：〇〇／携帯」を載せる
- 名前は安いAIで「人の名前らしいか」を見る。「営業担当」「スタッフ」や会社名・店舗名と同じものは入れ直しをお願いし、
  2回目も怪しければ登録は通して確認リスト（meta/name_review）に載せ、メールで知らせる（AIだけで断らない）
- 担当者名の変更は前回の変更から7日たってから。変更から24時間以内の入力ミスの直しは数えない
- 使い放題で目安の月300件を超えた方は、止めずに確認リストに載せて知らせる（運用で一度ご連絡）
"""
import json
import logging
import re
import unicodedata
from datetime import timedelta

import config
import store

log = logging.getLogger("person")

CHANGE_DAYS = 7
FIX_HOURS = 24
MOBILE_RE = re.compile(r"0[789]0-?\d{4}-?\d{4}")
NG_WORDS = ("担当", "スタッフ", "営業", "係", "店長", "不動産", "株式会社", "有限会社", "（株）", "(株)", "事務", "管理",
            "賃貸", "売買", "仲介", "サポート", "受付", "本部", "支店", "部", "課", "様", "さん", "テスト", "未定", "なし")
ADMIN_MAIL = config.env("ADMIN_MAIL", "m-aij@otpromo.com")

NAME_SYSTEM = """不動産会社の営業担当者が、サービスの登録で「担当者名」を入力しました。
これが実在の人の氏名（姓と名）らしいかを判定してください。外国籍の方の名前（カタカナ・ローマ字）や珍しい名前は「人の名前」とみなします。
役職・部署・「担当」「スタッフ」などの呼び方、会社名・店舗名、意味のない文字、姓だけ・名だけは「人の名前ではない」とします。
JSONだけで答えます：{"person": true か false, "reason": "20字以内"}"""


def normalize_name(t):
    t = unicodedata.normalize("NFKC", t or "").strip()
    return re.sub(r"\s+", " ", t)[:30]


def normalize_mobile(t):
    m = MOBILE_RE.search(unicodedata.normalize("NFKC", t or "").replace("ー", "-").replace("－", "-"))
    if not m:
        return None
    d = m.group().replace("-", "")
    return f"{d[:3]}-{d[3:7]}-{d[7:]}"


def _ai_says_person(name):
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_KEY, base_url="https://api.anthropic.com")
        resp = client.messages.create(model=config.env("NAME_MODEL", "claude-haiku-5-5"), max_tokens=100,
                                      system=NAME_SYSTEM, messages=[{"role": "user", "content": name}])
        t = "".join(b.text for b in resp.content if b.type == "text")
        obj, _ = json.JSONDecoder().raw_decode(t[t.index("{"):])
        return bool(obj.get("person")), obj.get("reason", "")
    except Exception:
        log.exception("名前の判定に失敗（通す）")
        return True, ""


def looks_suspicious(name, company):
    """人の名前らしくなければ理由の文字列、らしければ None。"""
    n = normalize_name(name)
    if len(n.replace(" ", "")) < 2:
        return "短すぎる"
    for k in ("会社名", "店舗名"):
        v = normalize_name((company or {}).get(k))
        if v and (n.replace(" ", "") == v.replace(" ", "") or n.replace(" ", "") in v.replace(" ", "")):
            return f"{k}と同じ"
    if any(w in n for w in NG_WORDS):
        return "役職・呼び方の言葉を含む"
    if re.search(r"\d", n):
        return "数字を含む"
    if config.MOCK_RESULT or not config.ANTHROPIC_KEY:
        return None  # 手元の確認ではAIを呼ばない
    ok, reason = _ai_says_person(n)
    return None if ok else (reason or "AIの判定")


# ---------- 変更の制限 ----------

def change_wait(u):
    """今は担当者名を変えられないなら、変えられるようになる日時。変えられるなら None。"""
    if not (u.get("company") or {}).get("担当者名"):
        return None
    last = u.get("name_changed_at")
    if not last:
        return None
    now = store.now()
    if now - last < timedelta(hours=FIX_HOURS) or now - last >= timedelta(days=CHANGE_DAYS):
        return None
    return last + timedelta(days=CHANGE_DAYS)


def set_name(uid, u, name):
    """担当者名を保存する。24時間以内の直しは、変更日を進めない。"""
    co = dict(u.get("company") or {})
    old = co.get("担当者名")
    co["担当者名"] = normalize_name(name)
    data = {"company": co, "name_retry": 0}
    last = u.get("name_changed_at")
    if old != co["担当者名"] and not (old and last and store.now() - last < timedelta(hours=FIX_HOURS)):
        data["name_changed_at"] = store.now()
    store.update_user(uid, data)


def try_name(uid, u, name):
    """名前を確かめて保存する。戻り値 ok / retry（入れ直しをお願いする）。2回目も怪しければ通して知らせる。"""
    reason = looks_suspicious(name, u.get("company"))
    if reason and not u.get("name_retry"):
        store.update_user(uid, {"name_retry": 1})
        return "retry"
    set_name(uid, u, name)
    if reason:
        flag(uid, store.get_user(uid), "担当者名の確認", f"「{normalize_name(name)}」（{reason}）")
    return "ok"


# ---------- 確認リストとお知らせ ----------

def flag(uid, u, kind, detail):
    """確認リストに載せ、運営にメールで知らせる。"""
    co = (u or {}).get("company") or {}
    item = {"uid": uid, "kind": kind, "detail": detail, "会社名": co.get("会社名", ""),
            "店舗名": co.get("店舗名", ""), "at": store.now()}

    def fn(m):
        m["items"] = (m.get("items") or [])[-499:] + [item]
    if store.db().get("meta", "name_review") is None:
        store.db().set("meta", "name_review", {"items": []})
    store.db().update_tx("meta", "name_review", fn)
    log.warning("確認リストに追加 %s %s %s", uid, kind, detail)
    try:
        import delivery
        delivery.send_notice(ADMIN_MAIL, f"【1分資料】{kind}",
                             f"{kind}\n\n会社名：{item['会社名']} {item['店舗名']}\n内容：{detail}\n利用者ID：{uid}\n"
                             f"日時：{store.now().astimezone(store.JST):%Y-%m-%d %H:%M}（日本時間）\n\n"
                             "AIの判定だけで断らず、必要なら担当からLINEで事情をうかがってください。")
    except Exception:
        log.exception("確認のメールに失敗")
