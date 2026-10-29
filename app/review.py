"""ストア審査用のアカウント（Google Play・App Store の審査担当が使う）。

審査担当は課金・お試しができないため、最初のログインから「使い放題」にし、内見FAX・受取りFAXの回数も十分に入れる。
会社情報・担当者名・携帯は最初から見本の値を入れる（変更も自由、名前の判定はしない）。紹介特典・日報や週報の人数からは外す。FAXは実際には送らず、送れたことにする（知らない相手に届かないように）。
"""
from datetime import timedelta

import config
import store

# 最初から入れておく会社情報（実在しない見本。名前の判定にかからない値）
COMPANY = {"会社名": "審査用サンプル不動産株式会社", "店舗名": "本店", "免許": "東京都知事（1）第00000号",
           "電話": "03-0000-0000", "FAX": "03-0000-0001", "住所": "東京都千代田区丸の内1丁目1番1号（審査用の見本）",
           "賃貸_仲介手数料_ヶ月": 1.0, "テーマ色": "青", "書体": "ゴシック", "担当者名": "山田 太郎",
           "ローマ字": "Taro Yamada", "役職": "", "携帯": "090-0000-0000", "携帯を載せる": True,
           "メール": "", "宛名を聞く": True, "帯画像": None, "名刺済み": True}
PACK = 100   # 回数券・追加FAXの回数
REFILL = 20  # これを下回ったら入れ直す


def is_review_email(email):
    return bool(email) and email.strip().lower() in config.REVIEW_EMAILS


def is_review(u):
    return bool(u and u.get("review"))


def ensure(uid, u):
    """審査用の状態にそろえる（ログインのたびに呼ぶ。そろっていれば何もしない）。"""
    import faxopt
    import naiken
    t = store.now()
    data = {}
    if not u.get("review") or (u.get("plan") or "free") != "unlimited" or u.get("plan_source") != "review":
        data.update({"review": True, "plan": "unlimited", "plan_source": "review", "billing_interval": "month"})
    if not u.get("period_end") or u["period_end"] <= t or data:
        data.update({"period_start": t, "period_end": t + timedelta(days=30)})
    if not u.get("registered"):
        data.update({"company": dict(COMPANY), "registered": True, "registered_at": t,
                     "delivery": {"method": "app", "email": "", "fax": COMPANY["FAX"]}})
    pack = {"n": PACK, "used": 0, "at": t, "exp": t + timedelta(days=90)}
    if faxopt.left(u) < REFILL:
        data["faxrecv_packs"] = [pack]
    if naiken.pack_left(u) < REFILL:
        data["fax_packs"] = [dict(pack)]
    if not data:
        return u
    store.update_user(uid, data)
    return store.get_user(uid)
