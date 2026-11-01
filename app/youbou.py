"""お問い合わせ・不具合のご連絡の窓口（2026-11-01 本人の質問「要望を受ける窓口はあるの？」から）。

・利用者に見える表示には「要望」という言葉を出さない（本人 11/1：無理な要望が来やすくなるため）。
  種類は「使い方の質問」「不具合のご連絡」「その他」。機能の追加にあたるものは、社内で「分類」を「要望」にするだけ
・メニュー「使い方・お問い合わせ」→「お問い合わせ・不具合のご連絡」→ 種類を選ぶ → 文や画像をこのトークに送る
・送られた内容は Firestore の requests に1件ずつ記録（種類・会社・担当・日時・本文・画像・対応状況）
・届いたら運営専用LINE（と運営のメール）にすぐ知らせ、月曜の週報に件数と一覧を載せる
・解約理由の質問の答え（growth.on_cancel_reason）も同じ requests に入れる
・受付の返事では、対応や実装の約束はしない
"""
import logging
import secrets
from datetime import timedelta

import config
import line
import store
import texts

log = logging.getLogger(__name__)

KINDS = {"shitsumon": "使い方の質問", "fuguai": "不具合", "sonota": "その他", "kaiyaku": "解約理由"}
# 社内の分類（運営が対応のときに付ける。利用者には見せない）：要望（機能の追加）／不具合／質問／その他
WAIT_MIN = 30          # 種類を選んでから、この分数のうちに届いた文・画像を受け付ける
MAX_IMAGES = 4         # 1件に付けられる画像の数
COL = "requests"


def _msg(mid, fallback, **kw):
    if mid in texts._MSG:
        return texts.build(mid, **kw)
    return line.text(fallback)


def active(u):
    w = (u or {}).get("req_wait") or {}
    return bool(w.get("kind") and w.get("at") and w["at"] >= store.now() - timedelta(minutes=WAIT_MIN))


def on_postback(c, v):
    """req=menu（種類を選ぶ）／req=shitsumon・fuguai・sonota（受付を始める）／req=done（送り終わり）。"""
    if v == "menu":
        return c.send(_msg("req_menu", "お問い合わせの内容を選んでください。"))
    if v in ("shitsumon", "fuguai", "sonota"):
        store.update_user(c.uid, {"req_wait": {"kind": v, "at": store.now(), "id": None}})
        return c.send(_msg(f"req_ask_{v}", "内容を、このトークにそのまま送ってください。"))
    if v == "done":
        store.update_user(c.uid, {"req_wait": None})
        return c.send(_msg("req_thanks", "ご連絡ありがとうございます。"))
    return c.send(_msg("req_menu", "お問い合わせの内容を選んでください。"))


def on_text(c, t):
    """受付中なら記録して True。"""
    if not active(c.u):
        return False
    _add(c, text=(t or "").strip()[:3000])
    return True


def on_image(c, message_id):
    """受付中の画像（不具合の画面など）。記録したら True（図面としては扱わない）。"""
    if not active(c.u):
        return False
    w = c.u["req_wait"]
    rid = w.get("id")
    doc = store.db().get(COL, rid) if rid else None
    if doc and len(doc.get("画像") or []) >= MAX_IMAGES:
        c.send(_msg("req_image_full", f"画像は1件につき{MAX_IMAGES}枚までお受けしています。"))
        return True
    try:
        data = line.content(message_id)
    except Exception:
        log.exception("ご要望の画像を受け取れませんでした")
        c.send(_msg("req_image_err", "申し訳ありません。画像を受け取れませんでした。もう一度送ってください。"))
        return True
    _add(c, image=data)
    return True


def _add(c, text=None, image=None):
    """1回の受付（種類を選んでから「送り終わり」まで）を1件にまとめる。最初の1通で運営へ知らせる。"""
    w = c.u["req_wait"]
    rid = w.get("id")
    doc = store.db().get(COL, rid) if rid else None
    first = doc is None
    if first:
        rid = new_id()
        doc = _base(c.uid, c.u, w["kind"])
    if text:
        doc["本文"] = (doc.get("本文") + "\n" if doc.get("本文") else "") + text
    if image:
        key = f"{COL}/{rid}/{len(doc.get('画像') or []) + 1}.jpg"
        store.db().put_file(key, image, "image/jpeg")
        doc["画像"] = (doc.get("画像") or []) + [key]
    doc["更新日時"] = store.now()
    store.db().set(COL, rid, doc)
    store.update_user(c.uid, {"req_wait": {**w, "id": rid, "at": store.now()}})
    c.refresh()
    if first:
        notify(doc)
        return c.send(_msg("req_received", "ご連絡ありがとうございます。"))
    return c.send(_msg("req_more", "追加でお受けしました。"))


def new_id():
    return store.now().astimezone(store.JST).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)


def _base(uid, u, kind):
    co = (u or {}).get("company") or {}
    return {"種類": KINDS.get(kind, kind), "kind": kind, "app": config.APP_MODE, "user_id": uid,
            "会社名": co.get("会社名", ""), "担当者名": co.get("担当者名", ""), "プラン": (u or {}).get("plan") or "free",
            "日時": store.now(), "本文": "", "画像": [], "対応状況": "未対応", "対応メモ": "",
            "分類": "不具合" if kind == "fuguai" else "解約理由" if kind == "kaiyaku" else ""}  # 要望は運営が分類を付ける


def record(uid, u, kind, text, notify_now=True):
    """会話を通さずに1件記録する（解約理由など）。"""
    rid = new_id()
    doc = {**_base(uid, u, kind), "本文": text}
    store.db().set(COL, rid, doc)
    if notify_now:
        notify(doc)
    return rid


def _app_name():
    return "1分資料 売買版" if config.APP_MODE == "sale" else "1分資料"


def notify(doc):
    import usage
    d = doc["日時"].astimezone(store.JST)
    text = (f"【{_app_name()}】{doc['種類']}が届きました\n"
            f"{doc.get('会社名') or '（会社名なし）'} {doc.get('担当者名') or ''}（{doc.get('プラン')}）\n"
            f"{d:%m/%d %H:%M}\n\n{(doc.get('本文') or '（画像のみ）')[:800]}")
    try:
        usage.admin_line(text)
    except Exception:
        log.exception("運営LINEへの通知")
    try:
        import delivery
        delivery.send_notice(usage.ADMIN_MAIL, f"【{_app_name()} {doc['種類']}】{doc.get('会社名') or ''}", text)
    except Exception:
        log.exception("運営メールへの通知")


def weekly_lines(days=7, limit=15):
    """週報に載せる行（直近7日の件数と一覧、未対応の合計）。"""
    since = store.now() - timedelta(days=days)
    rows = [(rid, d) for rid, d in store.db().all(COL) if d.get("日時") and d["日時"] >= since]
    rows.sort(key=lambda x: x[1]["日時"])
    open_all = sum(1 for _, d in store.db().all(COL) if d.get("対応状況", "未対応") == "未対応")
    cnt = {k: sum(1 for _, d in rows if d.get("kind") == k) for k in KINDS}
    youbou = sum(1 for _, d in rows if d.get("分類") == "要望")
    out = ["■お問い合わせ・不具合・解約理由（この1週間）",
           f"使い方の質問 {cnt['shitsumon']}件・不具合 {cnt['fuguai']}件・その他 {cnt['sonota']}件・解約理由 {cnt['kaiyaku']}件"
           f"（うち要望に分類 {youbou}件。未対応の合計 {open_all}件）"]
    for rid, d in rows[-limit:]:
        t = d["日時"].astimezone(store.JST)
        body = (d.get("本文") or "（画像のみ）").replace("\n", " ")
        out.append(f"・{t:%m/%d} {d.get('分類') or d['種類']}｜{d.get('会社名') or '-'}｜{body[:60]}{'…' if len(body) > 60 else ''}"
                   + (f"（画像{len(d['画像'])}枚）" if d.get("画像") else ""))
    if len(rows) > limit:
        out.append(f"ほか{len(rows) - limit}件")
    return "\n".join(out) + "\n"
