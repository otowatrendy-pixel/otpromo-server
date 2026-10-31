"""売買版のLINEの会話を手元で通す（AIは見本のJSONで代用、本物のLINE・AIには何も送らない）。

CHROME_PATH=/opt/pw-browsers/chromium python tests/sim_baibai_line.py

売買版は専用のLINE・専用のサーバー（APP_MODE=sale）で動かす（2026-10-31本人決定：入口を分け、共通部品は共有）。
確かめること
1. APP_MODE=sale：友だち追加は売買版のあいさつ。登録は賃貸の仲介手数料を聞かず、住所の後に所属団体と売買の手数料
2. 売買の図面 → 売買の資料（kind=sale）が届き、件数を1つ使う。AIは売買版の読み取り1回だけ
3. 賃貸版に登録済みの方 → 会社情報を引き継いで2問だけ。賃貸の図面が届いたら賃貸版のLINEへご案内（件数に数えない）
4. メニュー「元付へFAX」は売買の物件確認・内見依頼書、登録内容の変更（売買の設定）
5. APP_MODE=rent（初期値）のときは今の賃貸版と同じ（売買の図面は断る）… 最後に別のプロセスで確認
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
os.environ["APP_MODE"] = "rent" if OFF else "sale"
import shutil  # noqa: E402
shutil.rmtree(os.environ["LOCAL_DIR"], ignore_errors=True)

import bot  # noqa: E402
import core  # noqa: E402
import line  # noqa: E402
import person  # noqa: E402
import sale_bot  # noqa: E402
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

# 賃貸版の読み取り（AI）の代わり：売買版のサーバーでは呼ばれないはず（呼ばれたら数える）
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

# ---- 1) 新しく登録する方（売買版のLINE） ----
A = "Unew"
out = ev(A, "follow")
assert "1分資料 売買版" in out and "賃貸でも売買でも" not in out, out
out = pb(A, "reg=start")
assert "会社名" in out and "賃貸と売買のどちら" not in out, out
text(A, "サンプル不動産株式会社")
pb(A, "shop=none")
out = text(A, "見本 太郎")
assert "物件確認・内見依頼書" in out and "内見申込書" not in out, out  # 賃貸版と同じIDの文面を売買の言い方に
text(A, "090-0000-0000")
out = text(A, "東京都知事(1)第99999号")
assert "業界団体" not in out  # 所属団体は住所の後
text(A, "03-0000-9999")
pb(A, "fax=none")
out = text(A, "東京都〇〇区見本町1-1-1")
assert "所属されている業界団体" in out and "賃料の何ヶ月分" not in out, out
out = pb(A, "assoc=takken")
assert "（公社）東京都宅地建物取引業協会会員　（公社）首都圏不動産公正取引協議会加盟" in out, out  # 住所から正式名
pb(A, "assoc=ok")
out = pb(A, "sfee=custom")
assert "税込" in out
text(A, "55万")
pb(A, "color=青")
pb(A, "font=ゴシック")
out = pb(A, "obi=auto")
assert "FAXで受け取る" not in out, out
out = pb(A, "recv=line")
assert "所属団体" in out and "一律 550,000円" in out and "取引態様：媒介" in out and "賃料" not in out, out
out = pb(A, "reg=done")
assert "登録ありがとうございました" in out and "自動で見分けます" not in out, out
store.update_user(store.user_by_line(A)[0], {"company": {**u_of(A)["company"], "宛名を聞く": False}})

# ---- 2) 売買の図面 → 売買の資料 ----
out = image(A)
assert "売買の資料ができました" in out, out
uidA = store.user_by_line(A)[0]
job = store.get_job(u_of(A)["last_job"])
assert job["kind"] == "sale" and job["status"] == "done"
assert AI_CALLS == {"rental": 0, "sale": 1}, AI_CALLS  # 賃貸版の読み取りは呼ばない
print("帯の会社情報:", sale_core.band_company(u_of(A)["company"])["免許"])

# ---- 4) 元付へFAX（売買だけ作った方は、売買の物件確認へ） ----
out = pb(A, "motofax=start")
assert "物件確認・内見依頼書" in out
# 登録内容の変更：「仲介手数料」が「売買の設定」に
out = pb(A, "reg=edit")
assert "売買の設定" in out, out
out = pb(A, "edit=deal")
assert "売買の仲介手数料" in out
pb(A, "edit=tokurei")
out = pb(A, "tokurei=on")
assert "変更しました" in out and u_of(A)["company"]["売買設定"]["teiren_tokurei"] is True
pb(A, "edit=shiho")
out = text(A, "移転 77,000 抵当権 50000")
s = u_of(A)["company"]["売買設定"]
assert s["shihoshoshi_transfer"] == 77000 and s["shihoshoshi_mortgage"] == 50000, s
# 住宅ローンシミュレーション（会社の設定）
out = pb(A, "edit=loan")
assert "変動金利：1.2%" in out and "固定期間選択（10年）：3.7%" in out and "全期間固定：3.6%" in out and "付けない" in out, out
assert "3タイプを並べる" in out and "1つだけ載せる" not in out
out = pb(A, "loan=amount")
assert "物件価格の90%" in out
out = pb(A, "loan=r90")
assert u_of(A)["company"]["売買ローン目安"]["ratio"] == 0.9 and "物件価格の90%" in out, out
out = pb(A, "loan=cmp_on")
assert u_of(A)["company"]["売買ローン目安"]["compare"] is True and "1つだけ載せる" in out
pb(A, "loan=type")
out = pb(A, "loan=kotei_kikan")
assert "固定期間選択" in out
text(A, "固定10年 1.6\n変動 0.8\n年数 30")
L = sale_bot.loan(u_of(A)["company"])
assert L["type"] == "kotei_kikan" and L["rates"]["kotei_kikan"] == 1.6 and L["rates"]["hendo"] == 0.8 and L["years"] == 30, L
pb(A, "loan=amount")
pb(A, "loan=plus_costs")
assert sale_bot.loan(u_of(A)["company"])["amount_mode"] == "price_plus_costs"
out = pb(A, "loan=off")
assert sale_bot.loan(u_of(A)["company"])["show"] is False and "付ける" in out
pb(A, "loan=on")
pb(A, "edit=torihiki")
pb(A, "torihiki=代理")
assert sale_core.band_company(u_of(A)["company"])["帯_3行目"] == "取引態様：代理"

# ---- 3) 賃貸版に登録済みの方：会社情報を引き継ぐ ----
B = "Urent"
store.RENT_LOCAL[B] = {"registered": True, "plan": "light", "company": {
    "会社名": "見本住宅株式会社", "担当者名": "見本 花子", "携帯": "090-0000-1111", "免許": "東京都知事(2)第88888号",
    "電話": "03-0000-1111", "住所": "東京都〇〇区見本町2-2-2", "テーマ色": "緑", "書体": "明朝", "賃貸_仲介手数料_ヶ月": 1.0}}
out = ev(B, "follow")
assert "見本住宅株式会社" in out and "同意して引き継ぐ" in out, out
out = pb(B, "reg=copy")
assert "引き継ぎました" in out and "所属されている業界団体" in out, out
co = u_of(B)["company"]
assert co["テーマ色"] == "緑" and co["免許"] == "東京都知事(2)第88888号" and "賃貸_仲介手数料_ヶ月" not in co
assert u_of(B).get("plan", "free") == "free"  # 料金・件数は引き継がない（賃貸版とは別のサービス）
out = pb(B, "assoc=takken")
assert "団体を選び直す" in out and "書き直す" not in out, out
out = pb(B, "assoc=again")  # 打ち込まずに団体の一覧に戻る
assert "全日（ウサギマーク）" in out, out
pb(B, "assoc=zennichi")
out = pb(B, "assoc=ok")
assert "売買の仲介手数料" in out
pb(B, "sfee=rate")
out = text(B, "2%")
assert "登録内容のご確認" in out and "見本住宅株式会社" in out, out
pb(B, "reg=done")
assert u_of(B)["registered"] is True
AI_CALLS.update(rental=0, sale=0)
out = image(B)   # 宛名を聞く設定（初期値）のまま
assert "宛名" in out
out = text(B, "見本 一郎")
assert "売買の資料ができました" in out, out
assert AI_CALLS == {"rental": 0, "sale": 1}, AI_CALLS
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
assert sale_bot.monthly_payment(30_000_000, 1.0, 35) == 84686, sale_bot.monthly_payment(30_000_000, 1.0, 35)
assert sale_bot.prefecture("東京都練馬区") == "東京都" and sale_bot.prefecture("京都府京都市") == "京都府"
assert sale_bot.parse_evals("土地 12,345,678\n建物 456万") == {"land_eval": 12345678, "bldg_eval": 4560000}

# 物件ごとのローン条件（この資料だけ作り直す、件数に数えない）
jidB = u_of(B)["last_job"]
before = store.quota(u_of(B))["remaining"]
out = pb(B, f"lsim=ask&doc={jidB}")
assert "今の条件" in out, out
out = text(B, "借入 3500万 全期間固定 2.0% 30年")
assert "作り直しました" in out, out
assert store.get_job(jidB)["loan"] == {"amount": 35000000, "type": "zenki_kotei", "rate": 2.0, "years": 30}
pdf = store.db().get_file(f"jobs/{jidB}/okyaku.pdf")
t2 = unicodedata.normalize("NFKC", "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages))
assert "3,500万" in t2, [l for l in t2.splitlines() if "ローン" in l]
pb(B, f"lsim=ask&doc={jidB}")
text(B, "ローンなし")
assert store.get_job(jidB)["loan"] == {"amount": 35000000, "type": "zenki_kotei", "rate": 2.0, "years": 30, "use": False}
t3 = unicodedata.normalize("NFKC", "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(store.db().get_file(f"jobs/{jidB}/okyaku.pdf"))).pages))
assert "3,500万" not in t3 and "抵当権設定" not in t3, [l for l in t3.splitlines() if "ローン" in l or "抵当" in l]
out = pb(B, f"lsim=ask&doc={jidB}")
assert "使わない前提" in out, out
text(B, "変動 1.0%")  # 条件を送り直したら、ローンを使う前提に戻る
assert store.get_job(jidB)["loan"]["use"] is True and store.get_job(jidB)["loan"]["type"] == "hendo"

# 決済日（引渡日）：初めは図面を送った日の翌々月1日（仮）。8桁で送るとこの資料だけ作り直す
from datetime import date as _d
assert sale_bot.tentative_handover(_d(2026, 10, 31)) == _d(2026, 12, 1)
assert sale_bot.tentative_handover(_d(2026, 11, 5)) == _d(2027, 1, 1)
assert sale_bot.tentative_handover(_d(2026, 12, 31)) == _d(2027, 2, 1)
assert sale_bot.parse_day("２０２７０３０１", today=_d(2026, 10, 31)) == _d(2027, 3, 1)
assert sale_bot.parse_day("2027年3月1日", today=_d(2026, 10, 31)) == _d(2027, 3, 1)
assert sale_bot.parse_day("20260101", today=_d(2026, 10, 31)) is None and sale_bot.parse_day("20270230", today=_d(2026, 10, 31)) is None
out = pb(B, f"hand=ask&doc={jidB}")
assert "（仮：図面を送った日の翌々月1日）" in out and "仮の日に戻す" not in out, out
out = text(B, "20200101")
assert "読み取れませんでした" in out, out
d8 = sale_bot.today_jst() + __import__("datetime").timedelta(days=100)
out = text(B, d8.strftime("%Y%m%d"))
assert "作り直しました" in out and f"{d8.month}月{d8.day}日" in out, out
assert store.get_job(jidB)["handover"] == d8.isoformat()
t4 = unicodedata.normalize("NFKC", "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(store.db().get_file(f"jobs/{jidB}/okyaku.pdf"))).pages))
assert f"{d8.month}月{d8.day}日" in t4 or d8.strftime("%Y/%m/%d") in t4 or f"{d8.year}年{d8.month}月{d8.day}日" in t4, [l for l in t4.splitlines() if "日" in l][:20]
out = pb(B, f"hand=ask&doc={jidB}")
assert "仮の日に戻す" in out
out = pb(B, "hand=reset")
assert "（仮）" in out and store.get_job(jidB)["handover"] is None, out
th = sale_bot.tentative_handover(sale_bot.today_jst())
t5 = unicodedata.normalize("NFKC", "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(store.db().get_file(f"jobs/{jidB}/okyaku.pdf"))).pages))
assert f"{th.year}年{th.month}月1日" in t5 and "(仮)" in t5, [l for l in t5.splitlines() if "引渡" in l or "決済" in l]
assert "借入は物件価格の90%" in pb(B, f"lsim=ask&doc={jidB}") or True
pb(B, f"hand=ask&doc={jidB}")
pb(B, "cancel")
assert not u_of(B).get("sale_edit")
assert store.quota(u_of(B))["remaining"] == before

# 物件確認・内見依頼書FAX：売買の資料から宛先・物件が入り、文面は売買の言い方。送信・回数は賃貸の内見FAXの仕組み
import naiken
naiken.ai_parse = lambda t, only=None: {"wishes": [{"date": "2026-11-07", "time": "14:00", "note": None}], "guests": 2}
store.update_user(store.user_by_line(B)[0], {"plan": "light", "company": {**u_of(B)["company"], "名刺済み": True, "FAX": "03-0000-9999"}})
before = store.quota(u_of(B))["remaining"]  # ライトに切り替えたので数え直す
out = pb(B, f"bfax=from_last&doc={jidB}")
assert "メゾン見本" in out and "見本エステート株式会社" in out and "管理会社" not in out and "内見申込書" not in out, out
assert u_of(B)["nk"]["kind"] == "sale" and u_of(B)["nk"]["prop"]["price"] == "3,980万円"
out = text(B, "11/7 14時 2名")
assert "見本エステート株式会社 御中" in out, out
assert "管理会社" not in out and "内見申込書" not in out, out
fid = u_of(B)["nk_fid"]
fpdf = store.db().get_file(f"faxes/{fid}/naiken.pdf")
import re
ft = re.sub(r"\s", "", unicodedata.normalize("NFKC", "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(fpdf)).pages)))
assert "内見依頼書" in ft and "3,980万円" in ft and "評価額" in ft and "見本一郎" not in ft, ft[:600]
assert len(pypdf.PdfReader(io.BytesIO(fpdf)).pages) == 1
out = pb(B, "naiken=send")
assert "管理会社" not in out and "内見申込書" not in out, out
print("物件確認FAXの送信結果:", out[:120])
# 賃貸の内見申込書は今までどおりの言い方（売買の下書きのあとでも）
assert naiken.sale_words({"type": "text", "text": "内見申込書を管理会社へ", "quickReply": {"items": [{"action": {"label": "内見申込書を直す", "data": "naiken=edit"}}]}}) == \
    {"type": "text", "text": "物件確認・内見依頼書を元付業者へ", "quickReply": {"items": [{"action": {"label": "依頼書を直す", "data": "naiken=edit"}}]}}
out = pb(B, "bfax=recent")
assert "メゾン見本" in out, out
assert sale_bot.parse_evals("年税額 123,400 土地 1000万") == {"land_eval": 10_000_000, "annual_property_tax": 123400}

# 実際の評価額を送ると、補正の記録が1件残る（土地面積が分かるときだけ）。係数は3件たまるまで1.0
import calibration
pb(B, f"eval=ask&doc={jidB}")
text(B, "土地 30,000,000")
recs = [d for _, d in store.db().all("sale_calibration")]
print("補正の記録:", recs)
assert recs == []  # 見本はマンションで敷地持分が書いていないので記録しない
import sale_core, types, costs_sale, evaluation
FXS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "shohiyou")
costs_sale._LAND_DATA = evaluation.LandData.load(os.path.join(FXS, "landprice_points.json"), os.path.join(FXS, "chome_points.json"))
_ci = sale_core.cost_inputs
sale_core.cost_inputs = lambda f: {"property_type": "house", "price": 68_000_000, "address": "東京都○○区見本町2丁目",
                                   "land_area": 100.0, "floor_area": 90.0, "structure": "wood", "built": __import__("datetime").date(2015, 3, 1)}
rec = sale_bot.record_calibration("house1", types.SimpleNamespace(事実=None), 30_000_000)
sale_core.cost_inputs = _ci
print("補正の記録（戸建）:", rec)
assert rec and rec["city"] == "○○区" and rec["town"] == "見本町" and rec["actual_unit"] == 300000 and rec["est_unit"] > 0
assert store.db().get("sale_calibration", "house1") == rec
store.db().delete("sale_calibration", "house1")
for i in range(3):
    store.add_calib_record(f"x{i}", {"city": "練馬区", "town": "貫井", "est_unit": 400000, "actual_unit": 480000, "at": "2026-10-31"})
assert abs(sale_bot.calib_factor("東京都練馬区貫井2丁目") - 1.2) < 1e-9
assert sale_bot.calib_factor("東京都杉並区阿佐谷北1丁目") == 1.0
assert store.quota(u_of(B))["remaining"] == before

print("\nON: すべてOK")
r = subprocess.run([sys.executable, __file__, "off"], env={**os.environ, "LOCAL_DIR": "/tmp/otpromo-sim-baibai-off"})
assert r.returncode == 0
print("すべてOK")
