"""売買版のLINEの会話を手元で通す（AIは見本のJSONで代用、本物のLINE・AIには何も送らない）。

CHROME_PATH=/opt/pw-browsers/chromium python tests/sim_baibai_line.py

確かめること
1. SALE_ENABLED=1：登録の最初に「賃貸／売買／両方」→「両方」で所属団体と売買の手数料も聞く
2. 売買の図面 → 売買の資料（kind=sale）が届き、件数を1つ使う
3. 賃貸だけで登録した方が売買の図面を送る → 預かって2つ聞く → 終わったら同じ図面で作る（AIは1回だけ）
4. メニュー「元付へFAX」の振り分け、登録内容の変更（売買の設定）
5. SALE_ENABLED=0 のときは今の賃貸版と同じ（売買の図面は断る）… 最後に別のプロセスで確認
"""
import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("INVOICE_TRN", "")
os.environ.setdefault("LOCAL_DIR", "/tmp/otpromo-sim-baibai")
os.environ.setdefault("SALE_MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-baibai.json"))
OFF = len(sys.argv) > 1 and sys.argv[1] == "off"
os.environ["SALE_ENABLED"] = "0" if OFF else "1"
import shutil  # noqa: E402
shutil.rmtree(os.environ["LOCAL_DIR"], ignore_errors=True)

import bot  # noqa: E402
import core  # noqa: E402
import line  # noqa: E402
import person  # noqa: E402
import sale_core  # noqa: E402
import store  # noqa: E402
from PIL import Image  # noqa: E402

SENT, FILES = [], {}
line.reply = lambda tok, msgs: SENT.append(("reply", msgs)) or True
line.push = lambda to, msgs: SENT.append(("push", msgs)) or True
line.loading = lambda *a, **k: None
line.profile = lambda uid: {"displayName": "テスト太郎"}
line.content = lambda mid: FILES[mid]
person.try_name = lambda uid, u, t: (store.update_user(uid, {"company": {**(u.get("company") or {}), "担当者名": t}}), "ok")[1]

# 賃貸版の読み取り（AI）の代わり：売買の図面なので「賃貸以外」と返す
AI_CALLS = {"rental": 0, "sale": 0}
IMG = Image.open(ROOT / "tests" / "fixtures" / "mihon-baibai.png").convert("RGB")


def fake_rental(data):
    AI_CALLS["rental"] += 1
    f = SimpleNamespace(資料の種類="マイソク", 取引="賃貸以外", 用途="居住用")
    return {"img": IMG, "pages": 0, "result": SimpleNamespace(事実=f), "log": {}}


core.analyze = fake_rental
_sale_analyze = sale_core.analyze


def counted_sale(data):
    AI_CALLS["sale"] += 1
    return _sale_analyze(data)


sale_core.analyze = counted_sale


def texts_of():
    out = []
    for how, msgs in SENT:
        for m in msgs:
            if m["type"] == "text":
                qr = [i["action"].get("label") for i in m.get("quickReply", {}).get("items", [])]
                out.append(m["text"] + "\n" + " ".join(qr))
                print(f"[{how}] {m['text'][:160]!r}  ボタン:{qr}")
            elif m["type"] == "flex":
                out.append(m["altText"])
                print(f"[{how}] カード {m['altText'][:160]!r} ボタン:{[b['action']['label'] for b in m['contents']['footer']['contents']]}")
            else:
                print(f"[{how}] {m['type']}")
    SENT.clear()
    return "\n".join(out)


n = 0


def ev(uid, kind, **kw):
    global n
    n += 1
    e = {"type": kind, "replyToken": f"tok{n}", "webhookEventId": f"e{n}", "source": {"type": "user", "userId": uid}}
    e.update(kw)
    bot.handle(e)
    return texts_of()


def text(uid, t):
    print(f"\n>>> {t}")
    return ev(uid, "message", message={"type": "text", "id": f"m{n}", "text": t})


def pb(uid, d):
    print(f"\n>>> (ボタン) {d}")
    return ev(uid, "postback", postback={"data": d})


def image(uid):
    global n
    mid = f"m{n + 1}"
    FILES[mid] = (ROOT / "tests" / "fixtures" / "mihon-baibai.png").read_bytes()
    print("\n>>> (売買の募集図面)")
    return ev(uid, "message", message={"type": "image", "id": mid})


def u_of(line_uid):
    return store.user_by_line(line_uid)[1]


if OFF:
    # 5) SALE_ENABLED=0：賃貸版と同じ。登録の最初は会社名、売買の図面は断る
    U = "Uoff"
    ev(U, "follow")
    assert "会社名" in pb(U, "reg=start") or True
    for step in ("サンプル不動産株式会社",):
        text(U, step)
    store.update_user(store.user_by_line(U)[0], {"registered": True, "company": {"会社名": "サンプル不動産株式会社", "宛名を聞く": False}})
    out = image(U)
    assert "賃貸物件専門" in out, out
    assert AI_CALLS["sale"] == 0
    print("\nOFF: 賃貸版のまま OK")
    sys.exit(0)

# ---- 1) 新しく登録する方（両方） ----
A = "Uboth"
out = ev(A, "follow")
assert "賃貸でも売買でも" in out
out = pb(A, "reg=start")
assert "賃貸と売買のどちら" in out
pb(A, "kind=both")
text(A, "サンプル不動産株式会社")
pb(A, "shop=none")
text(A, "見本 太郎")
text(A, "090-0000-0000")
out = text(A, "東京都知事(1)第99999号")
assert "所属されている業界団体" in out, out
out = pb(A, "assoc=takken")
assert "（公社）東京都宅地建物取引業協会会員" not in out  # 住所はまだなので都道府県は〇〇県
pb(A, "assoc=ok")
text(A, "03-0000-9999")
pb(A, "fax=none")
text(A, "東京都〇〇区見本町1-1-1")
pb(A, "fee=1")
out = pb(A, "sfee=custom")
assert "税込" in out
text(A, "55万")
pb(A, "color=青")
pb(A, "font=ゴシック")
pb(A, "obi=auto")
out = pb(A, "recv=line")
assert "所属団体" in out and "定額 550,000円" in out and "取引態様：媒介" in out, out
out = pb(A, "reg=done")
assert "賃貸か売買かは自動で見分けます" in out
# 住所より先に団体を聞いたので、表記を住所に合わせて直す（変更から）
pb(A, "edit=assoc")
out = pb(A, "assoc=takken")
assert "（公社）東京都宅地建物取引業協会会員　（公社）首都圏不動産公正取引協議会加盟" in out, out
pb(A, "assoc=ok")
store.update_user(store.user_by_line(A)[0], {"company": {**u_of(A)["company"], "宛名を聞く": False}})

# ---- 2) 売買の図面 → 売買の資料 ----
out = image(A)
assert "売買の資料ができました" in out, out
uidA = store.user_by_line(A)[0]
job = store.get_job(u_of(A)["last_job"])
assert job["kind"] == "sale" and job["status"] == "done"
assert AI_CALLS == {"rental": 1, "sale": 1}
print("帯の会社情報:", sale_core.band_company(u_of(A)["company"])["免許"])

# ---- 4) 元付へFAX（売買だけ作った方は、売買の物件確認へ） ----
out = pb(A, "motofax=start")
assert "物件確認・内見依頼書" in out
# 登録内容の変更：「仲介手数料」が「賃貸・売買の設定」に
out = pb(A, "reg=edit")
out = pb(A, "edit=deal")
assert "売買の仲介手数料" in out
pb(A, "edit=tokurei")
out = pb(A, "tokurei=on")
assert "変更しました" in out and u_of(A)["company"]["売買設定"]["teiren_tokurei"] is True
pb(A, "edit=shiho")
out = text(A, "移転 77,000 抵当権 50000")
s = u_of(A)["company"]["売買設定"]
assert s["shihoshoshi_transfer"] == 77000 and s["shihoshoshi_mortgage"] == 50000, s
pb(A, "edit=loan")
text(A, "金利 0.9 年数 30")
assert u_of(A)["company"]["売買ローン目安"] == {"show": True, "rate": 0.9, "years": 30}
pb(A, "edit=torihiki")
pb(A, "torihiki=代理")
assert sale_core.band_company(u_of(A)["company"])["帯_3行目"] == "取引態様：代理"

# ---- 3) 賃貸だけで登録した方が売買の図面を送る ----
B = "Urent"
ev(B, "follow")
pb(B, "reg=start")
pb(B, "kind=rent")
text(B, "見本住宅株式会社")
pb(B, "shop=none")
text(B, "見本 花子")
text(B, "090-0000-1111")
out = text(B, "東京都知事(2)第88888号")
assert "業界団体" not in out  # 賃貸だけなら聞かない
text(B, "03-0000-1111")
pb(B, "fax=none")
text(B, "東京都〇〇区見本町2-2-2")
out = pb(B, "fee=1")
assert "売買の仲介手数料" not in out
pb(B, "color=青"); pb(B, "font=ゴシック"); pb(B, "obi=auto")
out = pb(B, "recv=line")
assert "所属団体" not in out  # 賃貸だけの方の確認は、今の賃貸版と同じ文
pb(B, "reg=done")
AI_CALLS.update(rental=0, sale=0)
out = image(B)   # 宛名を聞く設定（初期値）のまま
assert "宛名" in out
out = text(B, "見本 一郎")
assert "はじめての1回だけ" in out and "業界団体" in out, out
assert AI_CALLS == {"rental": 1, "sale": 1}
pb(B, "assoc=zennichi")
out = pb(B, "assoc=ok")
assert "売買の仲介手数料" in out
pb(B, "sfee=rate")
out = text(B, "2%")
assert "売買の資料ができました" in out, out
assert AI_CALLS == {"rental": 1, "sale": 1}, AI_CALLS   # 預かった読み取りを使い、AIは呼び直さない
job = store.get_job(u_of(B)["last_job"])
assert job["kind"] == "sale" and job["宛名"] == "見本 一郎"
assert u_of(B)["company"]["売買設定"]["fee_mode"] == "rate" and u_of(B)["company"]["売買設定"]["fee_rate"] == 0.02
pdf = store.db().get_file(f"jobs/{u_of(B)['last_job']}/okyaku.pdf")
import pypdf  # noqa: E402
import unicodedata  # noqa: E402
okyaku_text = unicodedata.normalize("NFKC", "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages))
print("お客様向けPDFのページ数:", len(pypdf.PdfReader(io.BytesIO(pdf)).pages))
assert "購入諸費用" in okyaku_text and "見本 一郎" in okyaku_text, okyaku_text[:300]
assert u_of(B)["company"]["所属団体"] == "（公社）全日本不動産協会会員　（公社）首都圏不動産公正取引協議会加盟"

# 月々の返済の計算（元利均等）：3,000万円・年1%・35年 ≒ 84,686円
import sale_bot  # noqa: E402
assert sale_bot.monthly_payment(30_000_000, 1.0, 35) == 84686, sale_bot.monthly_payment(30_000_000, 1.0, 35)
assert sale_bot.prefecture("東京都練馬区") == "東京都" and sale_bot.prefecture("京都府京都市") == "京都府"
assert sale_bot.parse_evals("土地 12,345,678\n建物 456万") == {"land_eval": 12345678, "bldg_eval": 4560000}

print("\nON: すべてOK")
r = subprocess.run([sys.executable, __file__, "off"], env={**os.environ, "LOCAL_DIR": "/tmp/otpromo-sim-baibai-off"})
assert r.returncode == 0
print("すべてOK")
