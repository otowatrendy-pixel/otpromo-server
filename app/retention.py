"""解約・ブロックした方の登録情報を1年後に消す（10/28本人決定）。

消すのは会社情報・担当者名・携帯・受け取り先・物件の記録などの登録情報。
請求の記録（Stripeの番号）と、無料お試しや紹介特典の重複を防ぐための印（使った件数・有料契約の有無）は残す。
"""
import logging
from datetime import timedelta

import store

log = logging.getLogger("retention")
KEEP_DAYS = 365
ERASE = ("company", "delivery", "naiken_history", "recent_jobs", "nk", "nk_fid", "reg_queue", "reg_mode",
         "reg_license_draft", "last_job", "last_naiken", "pending")


def _erase(uid, u, why):
    data = {k: None for k in ERASE if k in u}
    data.update({"registered": False, "purged_at": store.now(), "purged_reason": why})
    store.update_user(uid, data)
    log.info("登録情報を削除 %s（%s）", uid, why)


def daily():
    """定期実行から1日1回。消した人数を返す。"""
    cutoff = store.now() - timedelta(days=KEEP_DAYS)
    n = 0
    for field, why in (("ended_at", "解約から1年"), ("blocked_at", "ブロックから1年")):
        for uid, u in store.db().before("users", field, cutoff):
            if u.get("purged_at") and u["purged_at"] >= u[field]:
                continue
            if field == "ended_at" and (u.get("plan") or "free") != "free":
                continue  # また契約している
            if field == "blocked_at" and not u.get("blocked"):
                continue
            if (u.get("plan") or "free") != "free" and field == "blocked_at":
                continue  # ブロック中でも契約が続いている方は消さない
            _erase(uid, u, why)
            n += 1
    return n
