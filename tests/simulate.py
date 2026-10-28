"""LINEとのやりとりを手元でまねて、登録→マイソク→資料のお届けまでを通す（AIは見本のJSONで代用）。

STORE=local MOCK_RESULT=fixtures/mihon-chintai.json python tests/simulate.py
"""
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("INVOICE_TRN", "")  # 手元では Stripe に登録番号を作りに行かない
os.environ.setdefault("MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-chintai.json"))
os.environ.setdefault("LOCAL_DIR", "/tmp/otpromo-sim")

import bot  # noqa: E402
import line  # noqa: E402
import store  # noqa: E402

SENT = []
FILES = {}
line.reply = lambda tok, msgs: SENT.append(("reply", msgs)) or True
line.push = lambda to, msgs: SENT.append(("push", msgs)) or True
line.loading = lambda *a, **k: None
line.profile = lambda uid: {"displayName": "テスト太郎"}
line.content = lambda mid: FILES[mid]


def show():
    for how, msgs in SENT:
        for m in msgs:
            if m["type"] == "text":
                qr = [i["action"].get("label") for i in m.get("quickReply", {}).get("items", [])]
                print(f"[{how}] {m['text'][:int(os.environ.get('SHOW', '200'))]!r}  ボタン:{qr}")
            elif m["type"] == "flex":
                btn = [b["action"]["label"] for b in m["contents"]["footer"]["contents"]]
                print(f"[{how}] カード {m['altText'][:int(os.environ.get('SHOW', '120'))]!r} ボタン:{btn}")
            else:
                print(f"[{how}] {m['type']} {m.get('originalContentUrl', '')}")
    SENT.clear()


n = 0
def ev(kind, **kw):
    global n
    n += 1
    e = {"type": kind, "replyToken": f"tok{n}", "webhookEventId": f"e{n}", "source": {"type": "user", "userId": "Uabc"}}
    e.update(kw)
    bot.handle(e)
    show()


def text(t):
    print(f"\n>>> {t}")
    ev("message", message={"type": "text", "id": f"m{n}", "text": t})


def pb(d):
    print(f"\n>>> (ボタン) {d}")
    ev("postback", postback={"data": d})


def image(data, name=None):
    global n
    mid = f"m{n + 1}"
    FILES[mid] = data
    print(f"\n>>> ({'PDF ' + name if name else '画像'})")
    if name:
        ev("message", message={"type": "file", "id": mid, "fileName": name})
    else:
        ev("message", message={"type": "image", "id": mid})


img = (ROOT / "tests" / "fixtures" / "mihon-chintai.png").read_bytes()
print(">>> 友だち追加"); ev("follow")
image(img)                      # 登録前
pb("reg=start")
text("テスト不動産株式会社")
pb("shop=none")
text("営業担当")             # 人の名前らしくない → 入れ直しのお願い
text("山田 花子")
text("03-1234-5678")         # 携帯ではない → 入れ直しのお願い
text("090-1234-5678")
text("東京都知事(1)第12345号")
text("03-1234-5678")
pb("fax=none")
text("東京都練馬区豊玉北1-2-3")
pb("fee=1")
pb("color=紺")
pb("font=丸ゴシック")
pb("obi=auto")
pb("recv=line")
pb("reg=done")
image(img)

# PDF（2ページ）
from PIL import Image  # noqa: E402
im = Image.open(io.BytesIO(img)).convert("RGB")
buf = io.BytesIO(); im.save(buf, "PDF", save_all=True, append_images=[im])
image(buf.getvalue(), "maisoku.pdf")

text("FAX15")
pb("plan=status")
pb("send=mail")
text("test@example.com")
for _ in range(10):
    image(img)
pb("edit=color")
pb("color=code")
text("#FFDD00")
u = store.get_user("LUabc")
print("\n最終状態:", json.dumps({k: u[k] for k in ("plan", "free_limit", "free_used", "month_used", "company", "delivery", "pending")}, ensure_ascii=False, default=str))

# ---- Stripe の契約が始まったとき（Webhookの中身をまねる）----
import billing  # noqa: E402
import time as _t  # noqa: E402
store.update_user("LUabc", {"stripe_customer": "cus_test"})
sub = {"id": "sub_test", "customer": "cus_test", "status": "active", "metadata": {"uid": "LUabc"},
       "cancel_at_period_end": False,
       "items": {"data": [{"price": {"id": billing.config.PRICE_LIGHT},
                           "current_period_start": int(_t.time()), "current_period_end": int(_t.time()) + 30 * 86400}]}}
print("\n>>> (Stripe) ライト契約開始", billing.sync_subscription(sub))
bot.notify("LUabc", "plan_started", {})
bot.pool.shutdown(wait=True)
show()
print("プラン:", store.quota(store.get_user("LUabc")))
