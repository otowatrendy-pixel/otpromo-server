"""使い始めと続けてもらうためのお知らせ・週報（10/29本人承認、抜け漏れ点検表のQ4以外）。

1 登録して1件も送っていない方へ、登録の翌日と3日後に1回ずつ「まず1枚送ってみてください」（10〜18時）
2 無料の5件を使い切って有料にしていない方へ、3日後に1回だけライトの案内（10〜18時）
3 解約の手続きのあとに理由を1問（高い／使う機会がない／精度／他サービス／その他）。日報に件数
4 有料で14日使っていない方を日報に出し、使い方のコツを1回お知らせ
5 毎週月曜9時の週報：友だち追加→登録→初回送信→5件使用→有料→翌月継続の人数と、目標との差
文面は messages.json にあればそれを使い、なければここの文で送る。
"""
import logging
from datetime import datetime, timedelta

import line
import store
import texts
import usage

log = logging.getLogger("growth")
OPEN_HOUR, CLOSE_HOUR = 10, 18
IDLE_DAYS = 14
# 有料の方の人数の目標（月末、10/29本人承認）。2月末に30人未満なら見直し
TARGETS = {"202611": 10, "202612": 23, "202701": 38, "202702": 57}
REVIEW_LINE = ("202702", 30)
CANCEL_REASONS = {"price": "料金が高い", "nouse": "使う機会がない", "accuracy": "読み取りの精度",
                  "other_service": "ほかのサービスを使う", "other": "その他"}


def _msg(mid, fallback, buttons=None, **kw):
    if mid in texts._MSG:
        return texts.build(mid, **kw)
    m = line.text(fallback)
    if buttons:
        m["quickReply"] = {"items": [{"type": "action", "action": a} for a in buttons]}
    return m


def in_window(t=None):
    """受付時間内（日本時間10〜18時）か。"""
    h = (t or store.now()).astimezone(store.JST).hour
    return OPEN_HOUR <= h < CLOSE_HOUR


def next_window(t):
    """t 以降で、最初の受付時間内の時刻。"""
    j = t.astimezone(store.JST)
    if j.hour < OPEN_HOUR:
        j = j.replace(hour=OPEN_HOUR, minute=0, second=0, microsecond=0)
    elif j.hour >= CLOSE_HOUR:
        j = (j + timedelta(days=1)).replace(hour=OPEN_HOUR, minute=0, second=0, microsecond=0)
    return j


# ---------- 1・2 お知らせの予約 ----------

def on_registered(uid):
    """登録が終わったとき。翌日のお知らせを予約する。"""
    store.update_user(uid, {"nudge_step": 1, "nudge_at": next_window(store.now() + timedelta(days=1))})


def on_free_used_up(uid, u):
    """無料の件数を使い切ったとき。3日後のライトの案内を予約する（1回だけ）。"""
    if u.get("upsell_done") or u.get("upsell_at"):
        return
    store.update_user(uid, {"upsell_at": next_window(store.now() + timedelta(days=3))})


def run_due():
    """定期実行（5分ごと）から。予約の時刻が来たお知らせを送る。送った件数を返す。"""
    if not in_window():
        return 0
    n = 0
    now = store.now()
    for uid, u in store.db().before("users", "nudge_at", now):
        if not u.get("nudge_at"):
            continue
        step = u.get("nudge_step") or 1
        nxt = None
        if step == 1 and u.get("registered_at"):
            nxt = next_window(u["registered_at"] + timedelta(days=3))
        store.update_user(uid, {"nudge_at": nxt, "nudge_step": step + 1 if nxt else None})
        if u.get("total_docs") or u.get("free_used") or u.get("blocked") or not u.get("line_uid"):
            store.update_user(uid, {"nudge_at": None, "nudge_step": None})
            continue
        mid = "nudge_first_d1" if step == 1 else "nudge_first_d3"
        text = ("ご登録ありがとうございます。\nお手元の募集図面を、まず1枚撮って送ってみてください。1分ほどで、接客に使える資料が届きます。"
                if step == 1 else
                "その後いかがでしょうか。\n募集図面を1枚送っていただくだけで、帯替え図面や初期費用の概算書がそろいます。\n"
                "無料でお試しいただける分が、まだ残っています。")
        line.push(u["line_uid"], [_msg(mid, text, [{"type": "camera", "label": "カメラを開く"},
                                                   {"type": "cameraRoll", "label": "写真を選ぶ"}])])
        usage.count("nudge_" + str(step))
        n += 1
    for uid, u in store.db().before("users", "upsell_at", now):
        if not u.get("upsell_at"):
            continue
        store.update_user(uid, {"upsell_at": None, "upsell_done": True})
        if (u.get("plan") or "free") != "free" or u.get("blocked") or not u.get("line_uid"):
            continue
        import bot
        urls = bot.plan_urls(type("C", (), {"uid": uid})())
        if "nudge_free_used" in texts._MSG:
            msg = texts.build("nudge_free_used", urls=urls)  # ボタン2つ（ライト・使い放題の月払い）
        else:
            msg = texts.build("plan_show", urls=urls) if "plan_show" in texts._MSG else line.text("料金プランのご案内です。")
        line.push(u["line_uid"], [msg])
        usage.count("upsell")
        n += 1
    return n


# ---------- 3 解約の理由 ----------

def cancel_question():
    btns = [{"type": "postback", "label": v, "data": f"cancel_reason={k}", "displayText": v}
            for k, v in CANCEL_REASONS.items()]
    return _msg("cancel_reason_ask", "差し支えなければ、やめる理由を1つだけ教えてください。今後のサービスづくりに役立てます。", btns)


def on_cancel_reason(uid, key):
    if key not in CANCEL_REASONS:
        return None
    store.update_user(uid, {"cancel_reason": key, "cancel_reason_at": store.now(), "ask_cancel_reason": False})
    usage.count("cancel_" + key)
    return _msg("cancel_reason_thanks", "お答えいただき、ありがとうございました。\nまたお役に立てる機会がありましたら、いつでもお待ちしております。")


# ---------- 4 有料で14日使っていない方 ----------

def idle_paid(users=None):
    now = store.now()
    out = []
    for uid, u in users if users is not None else store.db().all("users"):
        if (u.get("plan") or "free") not in ("light", "unlimited") or u.get("blocked") or u.get("review"):
            continue
        last = u.get("last_doc_at") or u.get("first_paid_at") or u.get("period_start")
        if last and last <= now - timedelta(days=IDLE_DAYS):
            out.append((uid, u, last))
    return out


def send_idle_tips(idle):
    """使っていない有料の方へ、使い方のコツを1回だけ（同じ空白期間には2回送らず、60日に1回まで）。"""
    n = 0
    for uid, u, last in idle:
        if u.get("idle_tip_for") == last or not u.get("line_uid"):
            continue
        if u.get("idle_tip_at") and u["idle_tip_at"] > store.now() - timedelta(days=60):
            continue  # 同じ方には60日に1回まで
        store.update_user(uid, {"idle_tip_for": last, "idle_tip_at": store.now()})
        line.push(u["line_uid"], [_msg("idle_tip",
            "いつもご利用ありがとうございます。\n1分資料の使い方のコツをお届けします。\n\n"
            "・ポータルで見つけた物件も、募集図面のPDFを送るだけで資料になります\n"
            "・お客様向けのPDFは、そのままLINEで転送できます\n"
            "・業者向けの情報が残っていたら「〇〇を消して」と送ると、無料で直します")])
        n += 1
    return n


# ---------- 日報・週報 ----------

def daily_lines():
    """日報に足す行：読み取れなかった件数など、解約の理由、使っていない有料の方。"""
    y = usage._get("usage_d_" + usage._day(store.now() - timedelta(days=1)))
    m = usage._get("usage_m_" + usage._month())
    users = store.db().all("users")
    idle = idle_paid(users)
    sent = send_idle_tips(idle)
    reasons = "、".join(f"{v}{m.get('cancel_' + k, 0)}件" for k, v in CANCEL_REASONS.items())
    lines = [
        "■昨日の資料づくり",
        f"読み取れなかった件数：{y.get('read_fail', 0)}件（システムの失敗 {y.get('system_fail', 0)}件）",
        f"直しの依頼：{y.get('fix', 0)}件",
        f"FAXの失敗：{y.get('fax_fail', 0)}件",
        f"AI利用料（今月）：約{round(m.get('ai_yen', 0)):,}円／上限{usage.AI_STOP_YEN:,}円",
        "",
        f"■今月の解約の理由：{reasons}",
        "",
        f"■有料で{IDLE_DAYS}日使っていない方：{len(idle)}人（コツのお知らせを{sent}人に送りました）",
    ]
    for uid, u, last in idle[:10]:
        co = u.get("company") or {}
        lines.append(f"・{co.get('会社名', '')} {co.get('担当者名', '')}（最後に使った日 {last.astimezone(store.JST):%m/%d}）")
    return "\n".join(lines)


def funnel(users=None):
    users = users if users is not None else store.db().all("users")
    now = store.now()
    f = {"friends": 0, "registered": 0, "first_doc": 0, "five_docs": 0, "paid": 0, "continued": 0}
    for _, u in users:
        if u.get("review"):
            continue
        if u.get("line_uid") or u.get("firebase_uid"):
            f["friends"] += 1
        if u.get("registered"):
            f["registered"] += 1
        docs = max(u.get("total_docs", 0), u.get("free_used", 0))
        if docs >= 1:
            f["first_doc"] += 1
        if docs >= 5:
            f["five_docs"] += 1
        if u.get("paid_before") or (u.get("plan") or "free") != "free":
            f["paid"] += 1
        first = u.get("first_paid_at")
        if first and first <= now - timedelta(days=31) and (u.get("plan") or "free") != "free":
            f["continued"] += 1
    return f


def weekly_report():
    """毎週月曜9時の週報【1分資料 週報】。"""
    import delivery
    users = store.db().all("users")
    f = funnel(users)
    paid_now = sum(1 for _, u in users if (u.get("plan") or "free") in ("light", "unlimited") and not u.get("review"))
    t = store.now().astimezone(store.JST)
    key = t.strftime("%Y%m")
    goal = TARGETS.get(key)
    goal_line = (f"今月末の目標：{goal}人 → いま{paid_now}人（あと{max(0, goal - paid_now)}人）" if goal
                 else f"いまの有料の方：{paid_now}人")
    text = (f"{t.month}月{t.day}日（月）の週報です。\n\n"
            f"■人数の流れ（これまでの合計）\n"
            f"友だち追加 {f['friends']}人\n→ 登録 {f['registered']}人\n→ 初めて送った {f['first_doc']}人\n"
            f"→ 5件使った {f['five_docs']}人\n→ 有料 {f['paid']}人\n→ 翌月も継続 {f['continued']}人\n\n"
            f"■目標との差（有料の方）\n{goal_line}\n"
            "目標：11月末10人・12月末23人・1月末38人・2月末57人（2月末に30人未満なら見直し）\n")
    delivery.send_notice(usage.ADMIN_MAIL, "【1分資料 週報】", text)
    return {"funnel": f, "paid_now": paid_now, "goal": goal}
