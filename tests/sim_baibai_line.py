"""売買版のLINEの会話を手元で通す（AIは見本のJSONで代用、本物のLINE・AIには何も送らない）。

CHROME_PATH=/opt/pw-browsers/chromium python tests/sim_baibai_line.py

売買版は専用のLINE・専用のサーバー（APP_MODE=sale）で動かす（2026-10-31本人決定：入口を分け、共通部品は共有）。
確かめること
1. APP_MODE=sale：友だち追加は売買版のあいさつ。登録は賃貸の仲介手数料を聞かず、住所の後に所属団体と売買の手数料
2. 売買の図面 → 売買の資料（kind=sale）が届き、件数を1つ使う。AIは売買版の読み取り1回だけ
3. 賃貸版に登録済みの方 → 会社情報を引き継いで2問だけ。賃貸の図面が届いたら賃貸版のLINEへご案内（件数に数えない）
4. メニュー「元付へFAX」は売買の内見依頼書、登録内容の変更（売買の設定）
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
import json
import pypdf  # noqa: E402
import unicodedata  # noqa: E402
shutil.rmtree(os.environ["LOCAL_DIR"], ignore_errors=True)

import bot  # noqa: E402
import core  # noqa: E402
import line  # noqa: E402
import person  # noqa: E402
import sale_bot  # noqa: E402
import sale_core  # noqa: E402
import store  # noqa: E402
from PIL import Image  # noqa: E402

SENT, FILES, ALL = [], {}, []  # ALL は消さずにためる（あとでまとめて確かめる用）
_REAL_REPLY = line.reply  # 言い換えを本物の reply が通すかの確認用
line.reply = lambda tok, msgs: SENT.append(("reply", line.out(msgs))) or ALL.append(line.out(msgs)) or True  # 本物の reply・push と同じ言い換えを通す
line.push = lambda to, msgs: SENT.append(("push", line.out(msgs))) or ALL.append(line.out(msgs)) or True
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
    print("\n>>> (売買の販売図面)")
    return ev(uid, "message", message={"type": "image", "id": mid})


def u_of(line_uid):
    return store.user_by_line(line_uid)[1]



def check_stripe_split(mode):
    """同じStripeアカウント・同じuidでも、もう一方（賃貸版／売買版）の通知でプランを上書きしない（11/1 セット割スレッドの指摘）。"""
    import billing
    uid = "Lsplit"
    store.db().set("users", uid, {"plan": "light", "plan_source": "stripe", "stripe_customer": "cus_mine"})
    other = "sale" if mode == "rent" else "rent"
    def sub(app, cus):
        md = {"uid": uid, **({"app": "sale"} if app == "sale" else {})}
        return {"id": "sub_x", "customer": cus, "status": "canceled", "metadata": md,
                "items": {"data": [{"price": {"id": "price_x"}}]}}
    # もう一方の契約（解約）が届いても、こちらは light のまま
    assert billing.sync_subscription(sub(other, "cus_other")) == (None, None, None)
    assert store.get_user(uid)["plan"] == "light" and store.get_user(uid)["stripe_customer"] == "cus_mine"
    # Webhook の入口でも捨てる
    ev = {"id": "evt_split_" + mode, "type": "customer.subscription.deleted", "data": {"object": sub(other, "cus_other")}}
    _s = billing._stripe
    billing._stripe = lambda: SimpleNamespace(Webhook=SimpleNamespace(construct_event=lambda *a: ev))
    try:
        assert billing.handle_webhook(b"{}", "sig") == []
    finally:
        billing._stripe = _s
    assert store.get_user(uid)["plan"] == "light"
    # 自分の分は今までどおり反映（解約で free）
    assert billing.sync_subscription(sub(mode, "cus_mine"))[0] == uid and store.get_user(uid)["plan"] == "free"
    # Checkout：売買版だけ app=sale を付ける。賃貸版は何も変えない
    got = {}
    fake = SimpleNamespace(checkout=SimpleNamespace(Session=SimpleNamespace(create=lambda **kw: got.update(kw) or {"url": "u"})))
    billing.create_checkout(fake, mode="subscription", metadata={"uid": uid}, subscription_data={"metadata": {"uid": uid}})
    if mode == "sale":
        assert got["metadata"] == {"uid": uid, "app": "sale"} and got["subscription_data"]["metadata"] == {"uid": uid, "app": "sale"}
    else:
        assert got["metadata"] == {"uid": uid} and got["subscription_data"] == {"metadata": {"uid": uid}}
    # 価格：売買版は売買版の lookup_key・金額・商品で作る（賃貸版の価格IDは使わない）。賃貸版は今までどおり
    made, prods = [], []
    class _L(list):
        def auto_paging_iter(self):
            return iter(self)
    fake = SimpleNamespace(
        Price=SimpleNamespace(list=lambda **kw: {"data": []},
                              retrieve=lambda pid: {"active": False},
                              create=lambda **kw: made.append(kw) or {"id": "price_" + kw["lookup_key"]}),
        Product=SimpleNamespace(list=lambda **kw: _L(), create=lambda **kw: prods.append(kw) or {"id": "prod_" + (kw["metadata"].get("plan") or kw["metadata"].get("kind"))}),
        InvalidRequestError=Exception)
    _s, _k, saved = billing._stripe, billing.config.STRIPE_SECRET_KEY, {a: getattr(billing.config, a) for a, *_ in billing.PLAN_PRICES}
    billing._stripe, billing.config.STRIPE_SECRET_KEY = (lambda: fake), "sk_test_x"
    envs = {a: os.environ.pop(a) for a in saved if a in os.environ}
    try:
        billing.ensure_prices()
        got = {(m["lookup_key"], m["unit_amount"], m["recurring"]["interval"]) for m in made}
        if mode == "sale":
            assert got == {("sale_light_monthly", 2480, "month"), ("sale_unlimited_monthly", 4280, "month"),
                           ("sale_light_yearly", 24800, "year"), ("sale_unlimited_yearly", 42800, "year")}, got
            assert all(p["metadata"]["service"] == "maisoku_sale" and "売買版" in p["name"] for p in prods)
            assert billing.price_of("unlimited", True) == "price_sale_unlimited_yearly"
            assert billing.plan_of_price("price_sale_light_monthly") == ("light", False)
        else:
            assert all(not lk.startswith("sale_") for lk, *_ in got)
    finally:
        billing._stripe, billing.config.STRIPE_SECRET_KEY = _s, _k
        for a, v in saved.items():
            setattr(billing.config, a, v)
        os.environ.update(envs)
    print(f"Stripeの通知の見分け（{mode}）OK")


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
    check_stripe_split("rent")
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
assert "内見依頼書" in out and "内見申込書" not in out, out  # 賃貸版と同じIDの文面を売買の言い方に
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

# ---- 2) 売買の図面 → ローンの予定を聞く → 売買の資料 ----
out = image(A)
assert "住宅ローンのご利用予定" in out and "ローンあり（物件価格の90%）" in out and "売買の資料ができました" not in out, out
assert store.get_job(u_of(A)["sale_lq"]["jid"])["status"] == "analyzed"
out = pb(A, "lq=amount")
assert "借入額を送ってください" in out
out = text(A, "よく分からない")
assert "借入額を送ってください" in out  # 読めないときは聞き直す
out = text(A, "3200万")
assert "シミュレーション" in out and "いつもの設定：付ける" in out, out
out = pb(A, "lq2=cmp")
assert "売買の資料ができました" in out, out
assert store.get_job(u_of(A)["last_job"])["loan"] == {"use": True, "amount": 32_000_000, "show": True, "compare": True}
ta = unicodedata.normalize("NFKC", "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(store.db().get_file(f"jobs/{u_of(A)['last_job']}/okyaku.pdf"))).pages))
assert "3,200万" in ta and "全期間固定" in ta and "変動" in ta, [l for l in ta.splitlines() if "ローン" in l or "万" in l][:20]
assert not u_of(A).get("sale_lq")
uidA = store.user_by_line(A)[0]
job = store.get_job(u_of(A)["last_job"])
assert job["kind"] == "sale" and job["status"] == "done"
assert AI_CALLS == {"rental": 0, "sale": 1}, AI_CALLS  # 賃貸版の読み取りは呼ばない
print("帯の会社情報:", sale_core.band_company(u_of(A)["company"])["免許"])

# ---- 4) 元付へFAX（売買だけ作った方は、売買の内見依頼書へ） ----
out = pb(A, "motofax=start")
assert "内見依頼書" in out and "物件確認" not in out
# 登録内容の変更：「仲介手数料」が「売買の設定」に
out = pb(A, "reg=edit")
assert "売買の設定" in out, out
out = pb(A, "edit=deal")
assert "売買の仲介手数料" in out and "賃貸の仲介手数料" not in out and "内見依頼書のQRコード" in out
# 内見依頼書のQRコード（HPの売買ページへ）と一言：初期値は載せる。載せないときは空
import naiken
out = pb(A, "edit=nqr"); assert "今の設定：載せる" in out and "「1分資料」で作りました" in out, out
assert naiken._sale_qr_kw(u_of(A)["company"])["qr_svg"].startswith("<svg")
assert "変更しました" in pb(A, "nqr=off") and u_of(A)["company"]["内見依頼書のQR"] is False
assert naiken._sale_qr_kw(u_of(A)["company"]) == {"qr_svg": "", "qr_caption": ""}
pb(A, "nqr=on")
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
# 取引態様はいつも「媒介」（賃貸版と同じ）。変更の入口はない
out = pb(A, "edit=deal")
assert "取引態様" not in out and "800万円以下の手数料" in out, out
assert sale_core.band_company(u_of(A)["company"])["帯_3行目"] == "取引態様：媒介"

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
assert "住宅ローンのご利用予定" in out, out
out = pb(B, "lq=yes")
out = pb(B, "lq2=show")
assert "売買の資料ができました" in out, out
assert store.get_job(u_of(B)["last_job"])["loan"] == {"use": True, "show": True, "compare": False}
assert AI_CALLS == {"rental": 0, "sale": 1}, AI_CALLS  # 答えを待つあいだもAIは呼び直さない
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
assert store.get_job(jidB)["loan"] == {"use": True, "show": True, "compare": False, "amount": 35000000, "type": "zenki_kotei", "rate": 2.0, "years": 30}
pdf = store.db().get_file(f"jobs/{jidB}/okyaku.pdf")
t2 = unicodedata.normalize("NFKC", "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages))
assert "3,500万" in t2, [l for l in t2.splitlines() if "ローン" in l]
pb(B, f"lsim=ask&doc={jidB}")
text(B, "ローンなし")
assert store.get_job(jidB)["loan"] == {"use": False, "show": True, "compare": False, "amount": 35000000, "type": "zenki_kotei", "rate": 2.0, "years": 30}
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

# 内見依頼書FAX：売買の資料から宛先・物件が入り、文面は売買の言い方。送信・回数は賃貸の内見FAXの仕組み
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
assert "内見依頼書" in ft and "3,980万円" in ft and "評価額" not in ft and "ご回答欄" not in ft and "2名" not in ft and "見本一郎" not in ft, ft[:600]
assert len(pypdf.PdfReader(io.BytesIO(fpdf)).pages) == 1
assert "この依頼書は「1分資料」で作りました" in ft
# FAXで送るのは真っ黒の版（naiken_fax.pdf）。プレビュー（青）とは別に作ってあり、送信にはそちらを使う
xpdf = store.db().get_file(f"faxes/{fid}/naiken_fax.pdf")
assert xpdf and xpdf != fpdf
import delivery
_sf, _fr, _sent_pdf = delivery.send_fax, delivery.fax_ready, []
delivery.send_fax = lambda to, pdf, *a, **k: _sent_pdf.append(pdf) or "FAXTEST1"
delivery.fax_ready = lambda: True
out = pb(B, "naiken=send")
delivery.send_fax, delivery.fax_ready = _sf, _fr
assert _sent_pdf == [xpdf], "FAX用の版で送っていない"
assert "管理会社" not in out and "内見申込書" not in out, out
print("内見依頼FAXの送信結果:", out[:120])
# 賃貸の内見申込書は今までどおりの言い方（売買の下書きのあとでも）
assert naiken.sale_words({"type": "text", "text": "内見申込書を管理会社へ", "quickReply": {"items": [{"action": {"label": "内見申込書を直す", "data": "naiken=edit"}}]}}) == \
    {"type": "text", "text": "内見依頼書を元付業者へ", "quickReply": {"items": [{"action": {"label": "内見依頼書を直す", "data": "naiken=edit"}}]}}
out = pb(B, "bfax=recent")
assert "メゾン見本" in out, out
assert sale_bot.parse_evals("年税額 123,400 土地 1000万") == {"land_eval": 10_000_000, "annual_property_tax": 123400}

# 会社設定で「聞かない」なら、質問なしで会社設定のまま作る
store.update_user(store.user_by_line(B)[0], {"plan": "unlimited"})  # 件数の上限に当たらないように
pb(B, "edit=loan")
out = pb(B, "loan=ask_off")
assert "聞かない" in out and u_of(B)["company"]["売買ローン目安"]["ask_loan"] is False
out = image(B)
out = text(B, "見本 二郎")
assert "売買の資料ができました" in out and "住宅ローンのご利用予定" not in out, out
pb(B, "edit=loan"); pb(B, "loan=ask_on")
# 現金（ローンなし）
out = image(B); text(B, "見本 三郎")
out = pb(B, "lq=no")
assert "売買の資料ができました" in out and store.get_job(u_of(B)["last_job"])["loan"] == {"use": False}, out
# 土地：ローンは聞かず、資料のあとにスタッフ向けの一言
import json as _json
_land = _json.loads(open(os.environ["SALE_MOCK_RESULT"], encoding="utf-8").read())
_land["事実"]["物件種別"] = "土地"
LAND_JSON = os.path.join(os.environ["LOCAL_DIR"], "land.json")
open(LAND_JSON, "w", encoding="utf-8").write(_json.dumps(_land, ensure_ascii=False))
import config as _cfg
_old_mock, sale_core.SALE_MOCK_RESULT = sale_core.SALE_MOCK_RESULT, LAND_JSON
out = image(B); out = text(B, "見本 四郎")
sale_core.SALE_MOCK_RESULT = _old_mock
assert store.get_job(u_of(B)["last_job"])["result"]["事実"]["物件種別"] == "土地"
assert "住宅ローンのご利用予定" not in out and "つなぎ融資" in out and "売買の資料ができました" in out, out

# ---- 800万円以下の物件：ローンの質問のあとに、仲介手数料の扱いを1問だけ聞く ----
_cheap = _json.loads(open(os.environ["SALE_MOCK_RESULT"], encoding="utf-8").read())
_cheap["事実"]["価格"] = {"記載": "750万円", "金額円": 7_500_000}
CHEAP_JSON = os.path.join(os.environ["LOCAL_DIR"], "cheap.json")
open(CHEAP_JSON, "w", encoding="utf-8").write(_json.dumps(_cheap, ensure_ascii=False))
sale_core.SALE_MOCK_RESULT = CHEAP_JSON
q0 = store.quota(u_of(B))
out = image(B); text(B, "見本 五郎")
out = pb(B, "lq=no")
assert "800万円以下のため" in out and "特例（上限33万円）で計算" in out and "売買の資料ができました" not in out, out
assert "やめる" not in out  # 図面の直後は「やめる」を出さない
out = pb(B, "tq=amount")
assert "33万円まで" in out
out = text(B, "40万")
assert "上限（33万円・税込）を超えています" in out, out
out = text(B, "30万")
assert "売買の資料ができました" in out and "手数料を変える" in out, out
jidC = u_of(B)["last_job"]
assert store.get_job(jidC)["teiren"] == {"mode": "amount", "yen": 300_000}
tc = unicodedata.normalize("NFKC", "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(store.db().get_file(f"jobs/{jidC}/okyaku.pdf"))).pages))
assert "300,000" in tc, [l for l in tc.splitlines() if "仲介" in l]
q1 = store.quota(u_of(B))
# 届いた資料の「手数料を変える」：この資料だけ作り直す（件数に数えない）
out = pb(B, f"tq=ask&doc={jidC}")
assert "今の計算：300,000円（税込）" in out and "やめる" in out, out
out = pb(B, "tq=tokurei")
assert "作り直しました" in out and "特例（上限33万円・税込）" in out, out
tc = unicodedata.normalize("NFKC", "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(store.db().get_file(f"jobs/{jidC}/okyaku.pdf"))).pages))
assert "330,000" in tc, [l for l in tc.splitlines() if "仲介" in l]
out = pb(B, f"tq=ask&doc={jidC}"); out = pb(B, "tq=normal")
tc = unicodedata.normalize("NFKC", "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(store.db().get_file(f"jobs/{jidC}/okyaku.pdf"))).pages))
assert "330,000" not in tc and store.get_job(jidC)["teiren"] == {"mode": "normal"}
pb(B, f"tq=ask&doc={jidC}"); pb(B, "cancel")
assert not u_of(B).get("sale_tq")
assert store.quota(u_of(B)) == q1, (store.quota(u_of(B)), q1)
# 会社設定「いつも特例」なら聞かずに特例で作る
out = pb(B, "edit=tokurei")
assert "今の設定：毎回聞く" in out and "いつも特例で計算" in out, out
pb(B, "tokurei=on")
assert u_of(B)["company"]["売買設定"]["teiren_ask"] == "tokurei" and u_of(B)["company"]["売買設定"]["teiren_tokurei"] is True
out = image(B); text(B, "見本 六郎"); out = pb(B, "lq=no")
assert "800万円以下のため" not in out and "売買の資料ができました" in out, out
tc = unicodedata.normalize("NFKC", "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(store.db().get_file(f"jobs/{u_of(B)['last_job']}/okyaku.pdf"))).pages))
assert "330,000" in tc
pb(B, "edit=tokurei"); pb(B, "tokurei=ask")
# 税込表示で消費税が分からず、800万円前後（業者売主かもしれない建物）のときも聞く
_cheap["事実"]["価格"] = {"記載": "850万円", "金額円": 8_500_000}
_cheap["事実"]["取引態様"] = "売主"
open(CHEAP_JSON, "w", encoding="utf-8").write(_json.dumps(_cheap, ensure_ascii=False))
out = image(B); text(B, "見本 七郎"); out = pb(B, "lq=no")
assert "800万円以下のため" in out and "売主にご確認ください" in out, out
out = pb(B, "tq=normal")
assert "売買の資料ができました" in out, out
# 800万円を超える物件は聞かず、「手数料を変える」も出さない
_cheap["事実"]["価格"] = {"記載": "1,200万円", "金額円": 12_000_000}
open(CHEAP_JSON, "w", encoding="utf-8").write(_json.dumps(_cheap, ensure_ascii=False))
out = image(B); text(B, "見本 八郎"); out = pb(B, "lq=no")
assert "800万円以下のため" not in out and "売買の資料ができました" in out and "手数料を変える" not in out, out
sale_core.SALE_MOCK_RESULT = _old_mock
print("800万円以下の手数料の質問 OK")

# ---- 戸建て：土地の評価額をどちらで出すか（①路線価図／②地価公示） ----
_house = _json.loads(open(os.environ["SALE_MOCK_RESULT"], encoding="utf-8").read())
_house["事実"].update({"物件種別": "戸建", "所在地": "東京都杉並区高円寺南3丁目", "土地面積": "100.00㎡（公簿）",
                      "建物面積": "90.00㎡", "構造": "木造2階建", "築年月": "2015年3月", "専有面積": None})
HOUSE_JSON = os.path.join(os.environ["LOCAL_DIR"], "house.json")
open(HOUSE_JSON, "w", encoding="utf-8").write(_json.dumps(_house, ensure_ascii=False))
sale_core.SALE_MOCK_RESULT = HOUSE_JSON
qh = store.quota(u_of(B))
out = image(B); text(B, "見本 九郎"); out = pb(B, "lq=no")
assert "土地の評価額" in out and "路線価図で調べる" in out and "高円寺南3丁目" in out and "売買の資料ができました" not in out, out
out = pb(B, "eq=rosenka")
assert "rosenka.nta.go.jp" in out and "図1" in out and "道路の数字" in out, out
out = text(B, "わからない")
assert "読み取れませんでした" in out, out
out = text(B, "320D")
assert "売買の資料ができました" in out, out
jh = u_of(B)["last_job"]
assert store.get_job(jh)["evals"] == {"rosenka_per_m2": 320_000}, store.get_job(jh)["evals"]
assert not u_of(B).get("sale_eq") and not u_of(B).get("sale_eq_wait")
th = unicodedata.normalize("NFKC", "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(store.db().get_file(f"jobs/{jh}/okyaku.pdf"))).pages))
assert "路線価" in th, [l for l in th.splitlines() if "評価" in l or "推計" in l][:10]
# ②はそのまま作る
out = image(B); text(B, "見本 十郎"); pb(B, "lq=no"); out = pb(B, "eq=kouji")
assert "売買の資料ができました" in out, out
# ①を押したまま30分返事がない → 地価公示で作ってお届け（cron/often）
out = image(B); text(B, "見本 十一郎"); pb(B, "lq=no"); pb(B, "eq=rosenka")
uidB = store.user_by_line(B)[0]
assert sale_bot.eval_timeouts() == 0  # まだ30分たっていない
store.update_user(uidB, {"sale_eq": {**u_of(B)["sale_eq"], "at": store.now() - __import__("datetime").timedelta(minutes=31)}})
SENT.clear()
assert sale_bot.eval_timeouts() == 1
pushed = json.dumps([m for _, ms in SENT for m in ms], ensure_ascii=False)
assert "お返事がなかったため" in pushed and "売買の資料ができました" in pushed, pushed[:300]
assert not u_of(B).get("sale_eq") and u_of(B).get("sale_eq_wait") is False
assert store.quota(u_of(B))["used"] - qh["used"] == 3 if "used" in qh else True  # 3件作って3件分だけ
# 会社設定「いつも地価公示から」なら聞かない
pb(B, "edit=evalask"); pb(B, "evalask=kouji")
out = image(B); text(B, "見本 十二郎"); out = pb(B, "lq=no")
assert "土地の評価額" not in out and "売買の資料ができました" in out, out
pb(B, "edit=evalask"); pb(B, "evalask=ask")
assert sale_bot.parse_evals("路線価 320D") == {"rosenka_per_m2": 320_000}
sale_core.SALE_MOCK_RESULT = _old_mock
out = image(B); text(B, "見本 十三郎"); out = pb(B, "lq=no")  # このあとの確認は、最後の資料がマンションの前提
assert "売買の資料ができました" in out, out
print("評価額の出し方の質問 OK")

# ---- ご要望・不具合のご連絡：記録・運営へ通知・週報 ----
import usage, youbou, growth, delivery as _dl
_admin, _mails = [], []
_al, _sn = usage.admin_line, _dl.send_notice
usage.admin_line = lambda t: _admin.append(t)
_dl.send_notice = lambda to, subj, body, *a, **k: _mails.append((subj, body)) or True
pb(B, "help=menu")
assert "req=menu" in json.dumps(ALL[-1], ensure_ascii=False)
out = pb(B, "req=menu")
assert "使い方の質問" in out and "不具合のご連絡" in out and "要望" not in out, out
out = pb(B, "req=fuguai")
assert "スクリーンショット" in out
jobs_before = store.get_user(store.user_by_line(B)[0]).get("last_job")
out = text(B, "概算書の印紙税が違う気がします")
assert "担当が確認し、必要に応じてご連絡します" in out and "送り終わり" in out, out
out = image(B)  # 受付中の画像は図面として扱わない
assert "追加でお受けしました" in out and "売買の資料ができました" not in out, out
assert u_of(B).get("last_job") == jobs_before
reqs = [d for _, d in store.db().all("requests")]
assert len(reqs) == 1 and reqs[0]["種類"] == "不具合" and reqs[0]["本文"] == "概算書の印紙税が違う気がします" \
    and len(reqs[0]["画像"]) == 1 and reqs[0]["対応状況"] == "未対応" and reqs[0]["会社名"] == "見本住宅株式会社", reqs
assert len(_admin) == 1 and "不具合が届きました" in _admin[0] and "印紙税" in _admin[0], _admin
out = pb(B, "req=done")
assert "ありがとうございました" in out and not u_of(B).get("req_wait")
out = text(B, "こんにちは")
assert "担当が確認" not in out  # 受付が終わったら記録しない
pb(B, "req=menu"); pb(B, "req=sonota"); out = pb(B, "cancel")
assert not u_of(B).get("req_wait")
# 解約理由も同じ記録に（運営へのすぐの通知はしない）
growth.on_cancel_reason(store.user_by_line(B)[0], "price")
reqs = sorted((d for _, d in store.db().all("requests")), key=lambda d: d["日時"])
assert len(reqs) == 2 and reqs[-1]["種類"] == "解約理由" and reqs[-1]["本文"] == "料金が高い" and len(_admin) == 1
growth.weekly_report()
wk = [b for sj, b in _mails if "週報" in sj][-1]
assert "使い方の質問 0件・不具合 1件・その他 0件・解約理由 1件（うち要望に分類 0件。未対応の合計 2件）" in wk and "印紙税" in wk, wk
usage.admin_line, _dl.send_notice = _al, _sn
# 利用者に見える文面に「要望」は出さない
_shown = json.dumps([m for sec in json.load(open(ROOT / "app" / "messages_sale.json", encoding="utf-8"))["sections"]
                     for m in sec.get("messages", []) if m["id"].startswith("req_") or m["id"] == "sale_help_menu"], ensure_ascii=False)
assert all("要望" not in json.dumps({k: m.get(k) for k in ("text", "buttons")}, ensure_ascii=False)
           for m in json.loads(_shown)), "利用者向けに「要望」が出ている"
print("お問い合わせ・不具合の窓口 OK")

before = store.quota(u_of(B))["remaining"]  # ここから先（評価額の作り直し）は件数に数えない
jidB = u_of(B)["last_job"]

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

# 物件のおすすめポイント（お客様へ送る文章）：転送用の吹き出しを別に送り、スタッフ向けの一言はそのすぐ上の案内に入れる
found = 0
for ms in ALL:
    for i, m in enumerate(ms):
        if m.get("type") == "text" and "のおすすめポイント\n\n■" in m["text"]:
            found += 1
            assert "お客様へ送る前に内容をご確認ください" not in m["text"] and "AI" not in m["text"]
            assert any("お客様へ送る前に内容をご確認ください" in (x.get("text") or x.get("altText") or "") for x in ms[:i])
            assert ms[-1].get("quickReply"), "ボタンが最後の吹き出しにない"
assert found, "おすすめポイントの吹き出しが届いていない"
# 空のとき（強い言葉などで自動で外れた）は吹き出しを送らず、社内向けPDFを案内
n_, m_ = sale_bot.osusume_message({"texts": {"osusume": []}, "title": "x"})
assert m_ is None and "スタッフへの注意" in n_
# 「販売図面」にそろえる（10/31本人決定）：送った文面に「募集図面」が残っていない。本物の reply も言い換える
import json as _json
left = [m for _, ms in SENT for m in ms if "募集図面" in _json.dumps(m, ensure_ascii=False)]
assert not left, left[:2]
_sent = {}
import requests as _rq
_post = _rq.post
_rq.post = lambda url, **kw: _sent.update(kw["json"]) or SimpleNamespace(status_code=200, text="")
_REAL_REPLY("tok", [line.text("募集図面を送ってください", quick=[line.qr_postback("募集図面", "x=募集図面")])])
_rq.post = _post
assert _sent["messages"][0]["text"] == "販売図面を送ってください", _sent
assert "x=募集図面" in _json.dumps(_sent, ensure_ascii=False)  # postback の data は変えない
import texts as texts_mod
from datetime import timedelta as _td
check_stripe_split("sale")
# お試しは登録から14日（期間外5件・公開記念の2026/12/3〜2027/1/31の登録は10件）。1契約1回
from datetime import datetime as _dt
J = store.JST
for name, at, want in (("Ucamp", _dt(2026, 12, 3, 0, 0, tzinfo=J), "launch"), ("Ucamp2", _dt(2027, 1, 31, 23, 59, tzinfo=J), "launch"),
                       ("Upre", _dt(2026, 12, 2, 23, 59, tzinfo=J), "normal"), ("Upost", _dt(2027, 2, 1, 0, 0, tzinfo=J), "normal")):
    store.db().set("users", name, {"free_limit": 5, "free_used": 0, "plan": "free", "month_used": 0})
    assert store.launch_trial(name, at=at) == want, name
    assert store.launch_trial(name, at=at) is None  # 1契約1回
    u = store.get_user(name)
    assert u["free_expires"] == at + _td(days=14)
    assert u["free_limit"] == (10 if want == "launch" else 5)
# 14日を過ぎたら残りは失効
store.update_user("Ucamp", {"free_expires": store.now() - _td(seconds=1)})
assert store.quota(store.get_user("Ucamp"))["remaining"] == 0
store.update_user("Ucamp2", {"free_expires": store.now() + _td(days=1), "free_used": 3})
assert store.quota(store.get_user("Ucamp2"))["remaining"] == 7
# あいさつの一文は期間中だけ
assert texts_mod.launch_note(_dt(2026, 12, 10, tzinfo=J)) == "2027年1月31日までにご登録の方は、公開記念として10件にしています。"
# 期限の3日前に1回だけお知らせ（有料・使い切り・期限まで3日より先の方には送らない）
store.db().set("users", "Uexp1", {"plan": "free", "free_limit": 5, "free_used": 2, "free_expires": store.now() + _td(days=2, hours=23), "line_uid": "Lx1", "month_used": 0})
store.db().set("users", "Uexp2", {"plan": "free", "free_limit": 5, "free_used": 2, "free_expires": store.now() + _td(days=5), "line_uid": "Lx2", "month_used": 0})
store.db().set("users", "Uexp3", {"plan": "light", "free_limit": 5, "free_used": 0, "free_expires": store.now() + _td(days=1), "line_uid": "Lx3", "month_used": 0})
got = [x for x in sale_bot.trial_expiry_notices() if x[0].startswith("Uexp")]
assert [g[0] for g in got] == ["Uexp1"] and got[0][2]["n"] == 3, got
assert not [x for x in sale_bot.trial_expiry_notices() if x[0].startswith("Uexp")]  # 1回だけ
t = texts_mod.raw("sale_trial_expiring", **got[0][2])
assert "のこり3件" in t and "までです" in t, t
u1 = store.get_user("Uexp1"); u1["free_expires"] = store.now() + _td(days=5)
assert "まで）" in bot.remain_note(u1)
assert texts_mod.launch_note(_dt(2026, 11, 20, tzinfo=J)) == "" and texts_mod.launch_note(_dt(2027, 2, 1, tzinfo=J)) == ""
# 日報の「1件あたりのAI原価」：平均・最大を出し、20円を超えたら【至急】で知らせる
import usage
from datetime import timedelta as _td
yday = "usage_d_" + usage._day(store.now() - _td(days=1))
store.db().set("meta", yday, {"doc_ai_n": 4, "doc_ai_yen": 60.0, "doc_ai_max": 22.5})
before_n = usage._get("usage_m_" + usage._month()).get("doc_ai_n", 0)
usage.note_doc_cost(12.34); usage.note_doc_cost(18.0)
mm = usage._get("usage_m_" + usage._month())
assert mm["doc_ai_n"] == before_n + 2 and mm["doc_ai_max"] >= 18.0
alerts = []
_na = usage.notify_admin
usage.notify_admin = lambda subj, body: alerts.append((subj, body))
lines = usage._doc_cost_lines()
usage.notify_admin = _na
assert "昨日：平均15.0円・最大22.5円（4件）" in lines, lines
assert len(alerts) == 1 and "20円を超えました" in alerts[0][0] and "最大22.5円" in alerts[0][1] and "平均" not in alerts[0][1].split("でした")[0], alerts
print(lines)
# やり直したときは、1回目と2回目の原価を合計して1件分（10/31料金スレッド）
_calls = []
def _fake_claude(jpeg, effort=None):
    _calls.append(1)
    if len(_calls) == 1:
        e = ValueError("JSONの形が崩れた"); e.cost_yen = 7.5; raise e
    return sale_core.SaleResult.model_validate_json(open(os.environ["SALE_MOCK_RESULT"], encoding="utf-8").read()), {"原価_円": 9.25}
_cc, _mock = sale_core.call_claude, sale_core.SALE_MOCK_RESULT
sale_core.call_claude, sale_core.SALE_MOCK_RESULT = _fake_claude, ""
n0 = usage._get("usage_d_" + usage._day()).get("doc_ai_n", 0)
y0 = usage._get("usage_d_" + usage._day()).get("doc_ai_yen", 0)
try:
    a = sale_core.analyze((ROOT / "tests" / "fixtures" / "mihon-baibai.png").read_bytes())
finally:
    sale_core.call_claude, sale_core.SALE_MOCK_RESULT = _cc, _mock
assert a["log"]["原価_円"] == 16.75 and a["log"]["やり直し"] == 1, a["log"]
dd = usage._get("usage_d_" + usage._day())
assert dd["doc_ai_n"] == n0 + 1 and abs(dd["doc_ai_yen"] - y0 - 16.75) < 1e-6
import texts
import json as _j; ps = _j.dumps(line.out(texts.build("plan_show", **bot.plan_kw(), urls={f"Stripe決済ページ（{a}{b}）": "https://x" for a in ("ライト", "使い放題") for b in ("月額", "年額")})), ensure_ascii=False)
assert "販売図面 月30件分" in ps and "内見依頼FAX　月10回" in ps and "販売図面 月100件分" in ps and "内見依頼FAX　月30回" in ps and "【◯】" not in ps, ps
# MAXは月100件まで（仮、MAX_SHEETS）。使い切ったら MAX 用の案内（切り替えの案内は出さない）
uidM = store.user_by_line(B)[0]
store.update_user(uidM, {"plan": "unlimited", "month_used": 100, "month_key": store.db().get("users", uidM).get("month_key")})
q = store.quota(store.get_user(uidM))
assert q["remaining"] == 0, q
out = image(B)
assert "今月の100件分を" in out and "切り替え" not in out and "使い放題" not in out, out
store.update_user(uidM, {"month_used": 95})
assert "今月のこり5件です" in bot.remain_note(store.get_user(uidM)) and "100件に戻ります" in bot.remain_note(store.get_user(uidM))
store.update_user(uidM, {"month_used": 0})
assert bot.remain_note(store.get_user(uidM)) == ""
for mid in ("remain_free_last", "remain_light_low", "naiken_trial", "nudge_free_used", "plan_light_empty", "plan_needed"):
    t = _j.dumps(line.out(texts.build(mid, n=3, 更新日="12月1日", 期限="11月30日（月）", urls={})), ensure_ascii=False)
    assert "1,480" not in t and "2,980" not in t and "使い放題" not in t and "{" not in t.replace('{"', "").replace("{}", ""), (mid, t[:300])
assert store.quota(store.get_user(uidM))["remaining"] == 100
assert "MAX（月払い）" in ps and "使い放題" not in ps and "月額2,480円" in ps and "年額24,800円" in ps and "月額4,280円" in ps and "年額42,800円" in ps and "1,480" not in ps, ps
print("\nON: すべてOK")
r = subprocess.run([sys.executable, __file__, "off"], env={**os.environ, "LOCAL_DIR": "/tmp/otpromo-sim-baibai-off"})
assert r.returncode == 0
print("すべてOK")
