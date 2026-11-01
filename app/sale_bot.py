"""売買版のLINEの会話（賃貸版と同じLINEで、図面を見て振り分ける）。

SALE_ENABLED=0（初期値・本番）のあいだは、bot.py から呼ばれても何もしない＝賃貸版の動きは変わらない。
1 にすると:
- 登録の最初に「賃貸／売買／両方」を聞き、売買・両方なら所属団体と売買の仲介手数料を聞く
- 賃貸版の読み取りで「賃貸以外」と出た図面を、売買版でもう一度読み、売買の資料を届ける
- 賃貸だけで登録した方が売買の図面を送ったら、図面を預かって2つだけ聞き、終わったら続けて作る
- メニューの「元付へFAX」で、賃貸の内見申込書と売買の内見依頼書を振り分ける

文面は messages_sale.json（ID は sale_ で始まる）。
"""
import inspect
import io
import logging
import re
import unicodedata
from datetime import date, timedelta

import costs_sale
import config
import line
import store
import texts

log = logging.getLogger("sale_bot")

# 売買版は専用のLINE・専用のサーバー（APP_MODE=sale）で動かす（2026-10-31本人決定：入口を分け、共通部品は共有）。
# 賃貸版のサーバー（APP_MODE=rent、初期値）では売買の処理は一切動かない
SALE_ONLY = config.APP_MODE == "sale"
ENABLED = SALE_ONLY
# 試験公開で売買を開ける方（LINEのユーザーIDかサーバーのID、カンマ区切り）。空なら全員
ALLOW = {x.strip() for x in config.env("SALE_ALLOW_UIDS").split(",") if x.strip()}

# 売買の設定の初期値（costs_sale.CompanySaleSettings と同じ名前）。会社ごとに company["売買設定"] で上書き
DEFAULT_SETTINGS = {"fee_mode": "max", "fee_custom": None, "fee_rate": None, "teiren_tokurei": False, "estimate_eval": True,
                    "eval_ask": "ask",  # 戸建て・土地の評価額：ask＝①路線価図／②地価公示を聞く、kouji＝いつも②
                    # 800万円以下の物件の手数料：ask＝図面ごとに聞く（初期値）／normal＝いつも通常／tokurei＝いつも特例
                    "teiren_ask": "ask",
                    # 司法書士報酬・実費の初期値は概算書の計算側（costs_sale.CompanySaleSettings）を唯一の元にする
                    **{k: getattr(costs_sale.CompanySaleSettings(), k) for k in
                       ("shihoshoshi_transfer", "shihoshoshi_preserve", "shihoshoshi_mortgage", "touki_jippi")}}
# 月々の返済の目安（概算書に小さく入れる）。金利は仮置き（本人確認待ち）
# 住宅ローンシミュレーション（概算書に付ける）。金利は仮置き（本人確認待ち）。項目名は概算書スレッドとそろえる
LOAN_TYPES = {"hendo": "変動金利", "kotei_kikan": "固定期間選択", "zenki_kotei": "全期間固定"}
# 初期値は概算書の計算側（sale_estimate.LOAN_DEFAULTS）を唯一の元にする。ここで別の値を持たない
try:
    from sale_estimate import LOAN_DEFAULTS as DEFAULT_LOAN
except ImportError:  # 概算書のモジュールが無い環境（テスト用の最小構成など）
    DEFAULT_LOAN = {"use": True, "show": True, "type": "hendo", "rates": {"hendo": 1.2, "kotei_kikan": 3.7, "zenki_kotei": 3.6},
                    "kotei_years": 10, "years": 35, "amount_mode": "ratio", "ratio": 0.9, "amount": None, "compare": False}
FEE_NAMES = {"max": "上限どおり（価格×3%＋6万円・税別）", "half": "上限の半額", "free": "無料", "custom": "一律の金額"}


def on(c=None):
    """この方に売買を開けているか。"""
    if not ENABLED:
        return False
    if not ALLOW or c is None:
        return True
    return c.uid in ALLOW or c.line_uid in ALLOW


# ---------- 所属団体（帯の表記） ----------------------------------------------------------

PREFS = ("北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県", "埼玉県",
         "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県", "静岡県",
         "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県",
         "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県", "熊本県", "大分県",
         "宮崎県", "鹿児島県", "沖縄県")
# 首都圏不動産公正取引協議会の地域（1都8県）
SHUTOKEN = {"東京都", "神奈川県", "埼玉県", "千葉県", "茨城県", "栃木県", "群馬県", "山梨県", "長野県"}
ASSOC = {
    "takken": "（公社）{pref}宅地建物取引業協会会員",
    "zennichi": "（公社）全日本不動産協会会員",
    "frk": "（一社）不動産流通経営協会会員",
    "zenjukyo": "（一社）全国住宅産業協会会員",
}


def prefecture(address):
    a = unicodedata.normalize("NFKC", address or "").strip()
    for p in PREFS:
        if a.startswith(p):
            return p
    # 「東京」「大阪」のように都府県を省いた書き方
    for p in PREFS:
        if a.startswith(p[:-1]) and p[:-1] not in ("京都",):
            return p
    return None


def assoc_text(kind, address):
    """選んだ団体から帯の表記を作る。都道府県が分からない・首都圏の外は、公取協を空にして確認で直してもらう。"""
    pref = prefecture(address)
    if kind == "none":
        return ""
    head = ASSOC[kind].format(pref=pref or "〇〇県")
    kotori = "（公社）首都圏不動産公正取引協議会加盟" if pref in SHUTOKEN else "（〇〇不動産公正取引協議会加盟）"
    return f"{head}　{kotori}"


def needs_setup(u):
    """売買の資料に要る登録が足りないか（所属団体を一度も聞いていない・売買の手数料が未設定）。"""
    co = u.get("company") or {}
    return "所属団体" not in co or "売買設定" not in co


def settings(co):
    return {**DEFAULT_SETTINGS, **(co.get("売買設定") or {})}


def loan(co):
    """会社の住宅ローンシミュレーションの設定（初期値で埋める）。rate は今のタイプの金利（互換のため）。"""
    saved = dict(co.get("売買ローン目安") or {})
    L = {**DEFAULT_LOAN, **saved, "rates": {**DEFAULT_LOAN["rates"], **(saved.get("rates") or {})}}
    if "rate" in saved and "rates" not in saved:  # 古い形（rate だけ）は今のタイプの金利として読む
        L["rates"][L["type"]] = float(saved["rate"])
    L["rate"] = L["rates"].get(L["type"], DEFAULT_LOAN["rates"]["hendo"])
    return L


def loan_label(L):
    t = LOAN_TYPES.get(L.get("type"), "変動金利")
    if L.get("type") == "kotei_kikan":
        t += f"（{L.get('kotei_years', 10)}年）"
    return f"{t} 年{float(L['rate']):g}%・{L['years']}年"


def amount_label(L):
    if L.get("amount_mode") == "price_plus_costs":
        return "物件価格＋諸費用"
    r = float(L.get("ratio", 1.0))
    return "物件価格の全額" if r >= 1 else f"物件価格の{r * 100:g}%"


def fee_label(co):
    s = settings(co)
    if s["fee_mode"] == "rate" and s.get("fee_rate"):
        return f"価格（税抜）の{s['fee_rate'] * 100:g}%＋消費税（上限まで）"
    if s["fee_mode"] == "custom" and s.get("fee_custom"):
        return f"一律 {s['fee_custom']:,}円（税込）"
    return FEE_NAMES.get(s["fee_mode"], FEE_NAMES["max"])


# ---------- 登録の質問（bot.py の REG_STEPS に差し込む） -----------------------------------

ASK = {"kind": "sale_reg_kind", "assoc": "sale_reg_assoc", "assoc_other": "sale_reg_assoc_other",
       "assoc_check": "sale_reg_assoc_check", "sfee": "sale_reg_fee", "sfee_custom": "sale_reg_fee_custom",
       "sfee_rate": "sale_reg_fee_rate"}


def reg_steps(base, kind):
    """賃貸版の登録の質問（base）から、選んだ使い方の質問の並びを作る。
    kind: rent（今と同じ）/ sale（賃貸の手数料を聞かない）/ both（全部）。"""
    steps = [s for s in base if not (kind == "sale" and s == "fee")]
    if kind == "sale":  # 所属団体は住所の後に聞く（都道府県から正式な団体名を作れるので、打ち込みも直しも要らない）
        i = steps.index("address") + 1
        return steps[:i] + ["assoc", "sfee"] + steps[i:]
    if kind == "both":
        steps.insert(steps.index("license") + 1, "assoc")
        after = "fee" if "fee" in steps else "address"
        steps.insert(steps.index(after) + 1, "sfee")
    return steps


def ask_message(c, step):
    co = c.u.get("company") or {}
    if step == "assoc_check":
        return texts.build("sale_reg_assoc_check", 帯の表記=co.get("所属団体") or "（表記なし）")
    return texts.build(ASK[step])


def set_company(c, **kv):
    co = dict(c.u.get("company") or {})
    co.update(kv)
    store.update_user(c.uid, {"company": co})
    c.refresh()


def set_settings(c, **kv):
    co = c.u.get("company") or {}
    set_company(c, 売買設定={**(co.get("売買設定") or {}), **kv})


def reg_postback(c, step, k, v):
    """登録中のボタン。戻り値: ("next", 差し込む質問) か ("ask", 聞き直す質問) か None（売買の質問ではない）。"""
    if k == "kind" and step == "kind":
        store.update_user(c.uid, {"use_kind": v})
        return ("rebuild", v)
    if k == "assoc" and step in ("assoc", "assoc_check"):
        if v == "other":
            return ("next", "assoc_other")
        if v == "again":  # 表記を見て「団体を選び直す」→ 団体の一覧に戻る（打ち込まずに済むように）
            return ("ask", "assoc")
        if v == "ok":
            return ("next", None)
        if v not in ASSOC and v != "none":
            return ("ask", step)
        set_company(c, 所属団体=assoc_text(v, (c.u.get("company") or {}).get("住所")), 所属団体_種類=v)
        return ("next", None if v == "none" else "assoc_check")
    if k == "sfee" and step == "sfee":
        if v == "custom":
            return ("next", "sfee_custom")
        if v == "rate":
            return ("next", "sfee_rate")
        if v not in ("max", "half", "free"):
            return ("ask", step)
        set_settings(c, fee_mode=v, fee_custom=None)
        return ("next", None)
    return None


def reg_text(c, step, t):
    """登録中の文字。戻り値は reg_postback と同じ。"""
    t = unicodedata.normalize("NFKC", t or "").strip()
    if step == "assoc_other":
        set_company(c, 所属団体=t[:80], 所属団体_種類="other")
        return ("next", None)
    if step in ("assoc", "assoc_check"):
        # ボタンを押さずに団体名を書いてきたときは、そのまま表記として受け取る
        set_company(c, 所属団体=t[:80], 所属団体_種類="other")
        return ("next", None)
    if step == "sfee_rate":
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[%％]?", t)
        if not m or not (0 < float(m.group(1)) <= 3):
            return ("ask", "sfee_rate")
        set_settings(c, fee_mode="rate", fee_rate=round(float(m.group(1)) / 100, 4), fee_custom=None)
        return ("next", None)
    if step in ("sfee", "sfee_custom"):
        n = parse_yen(t)
        if n is None or not (0 <= n <= 10_000_000):
            return ("ask", "sfee_custom")
        set_settings(c, fee_mode="free" if n == 0 else "custom", fee_custom=n or None)
        return ("next", None)
    return None


def parse_yen(t):
    s = unicodedata.normalize("NFKC", t or "").replace(",", "").replace("円", "").replace("税込", "").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(万)?", s)
    if not m:
        return None
    n = float(m.group(1)) * (10_000 if m.group(2) else 1)
    return int(round(n))


def confirm_fields(u):
    """sale_reg_confirm に差し込む売買の行。"""
    co = u.get("company") or {}
    kind = u.get("use_kind") or "both"
    rent = "" if kind == "sale" else f"賃貸の仲介手数料：賃料の{co.get('賃貸_仲介手数料_ヶ月', 1.0):g}ヶ月分（税別）\n"
    return {"所属団体": co.get("所属団体") or "なし", "賃貸の手数料行": rent, "売買手数料": fee_label(co),
            "取引態様": "媒介"}  # 帯の取引態様は賃貸版と同じく、いつも「媒介」（2026-11-01 本人指示）


# ---------- 売買の図面が届いたとき ----------------------------------------------------------

def handle_sale(c, data, jid, rental_a=None):
    """賃貸版の読み取りで「賃貸以外」と出た図面。売買版で読み直して資料を届ける。
    戻り値は送ったメッセージ（bot.process の続きは不要）。"""
    import sale_core
    if not on(c):
        store.update_job(jid, {"status": "failed", "error": "sale"})
        return c.send(texts.build("sale_err_disabled" if ENABLED else "err_sale"))
    if rental_a is not None and not sale_core.route_from_rental(rental_a["result"]):  # 同じLINEで受けていたころの名残
        store.update_job(jid, {"status": "failed", "error": "not_maisoku"})
        return c.send(texts.build("err_not_maisoku"))
    a = sale_core.analyze(data)
    v = sale_core.verdict(a["result"])
    if v != "ok":
        err = f"sale_unsupported:{a['result'].事実.物件種別 or ''}" if v == "unsupported" else f"sale_{v}"
        store.update_job(jid, {"status": "failed", "error": err, "log": a["log"]})
        return c.send(error_message(err))
    if needs_setup(c.u):
        hold_for_setup(c, jid, a)
        return
    if ask_next(c, jid, a):
        return
    return finish(c, jid, a)


def error_message(err):
    """売買版で作れなかったときの文（err は "sale_<判定>" か "sale_unsupported:<種別>"）。"""
    code, _, kind = err[5:].partition(":")
    if code == "unsupported":
        return texts.build("sale_err_unsupported", 物件種別=kind or "この種類")
    if code == "disabled":
        return texts.build("sale_err_disabled")
    if code == "rental":
        return texts.build("sale_err_rental", 賃貸版のLINE=config.RENT_LINE_URL)
    return texts.build({"blurry": "err_blurry", "not_maisoku": "err_not_maisoku"}.get(code, "err_system"))


def _save_analyzed(jid, a, **extra):
    import sale_schema
    buf = io.BytesIO()
    a["img"].save(buf, "PNG")
    store.db().put_file(f"jobs/{jid}/analyzed.png", buf.getvalue(), "image/png")
    store.update_job(jid, {"status": "analyzed", "kind": "sale", "result": sale_schema.for_store(a["result"]),
                           "pages": a["pages"], "log": a["log"], **extra})


def _load_analyzed(jid, job):
    import sale_schema
    from PIL import Image
    png = store.db().get_file(f"jobs/{jid}/analyzed.png")
    return {"img": Image.open(io.BytesIO(png)).convert("RGB"), "pages": job.get("pages") or 0,
            "result": sale_schema.SaleResult.model_validate(job["result"]), "log": job.get("log") or {}}


def analyze_for_atena(c, jid, data, rental_a):
    """宛名を聞いているあいだに、売買版で読み直して預かる（bot.analyze_and_ask から）。宛名が届いたら after_atena。"""
    import sale_core
    if rental_a is not None and not sale_core.route_from_rental(rental_a["result"]):
        return store.update_job(jid, {"status": "failed", "error": "not_maisoku"})
    a = sale_core.analyze(data)
    v = sale_core.verdict(a["result"])
    if v != "ok":
        err = f"sale_unsupported:{a['result'].事実.物件種別 or ''}" if v == "unsupported" else f"sale_{v}"
        return store.update_job(jid, {"status": "failed", "error": err, "log": a["log"]})
    _save_analyzed(jid, a)


def after_atena(c, jid, job, atena, after=()):
    """宛名が届いた売買の図面。設定が足りなければ先に2つ聞き、そろっていれば作る。"""
    if needs_setup(c.u):
        store.update_job(jid, {"宛名": atena})
        store.update_user(c.uid, {"sale_setup_job": jid, "sale_setup_at": store.now(),
                                  "reg_queue": ["assoc", "sfee"], "reg_mode": "sale_setup"})
        c.refresh()
        return c.send(texts.build("sale_need_setup"), texts.build("sale_reg_assoc"))
    a = _load_analyzed(jid, job)
    if not after and ask_next(c, jid, a, atena, saved=True):  # 何枚か続けて送られたときは聞かず、会社設定のまま作る
        return
    return finish(c, jid, a, atena, after)


def hold_for_setup(c, jid, a):
    """所属団体などを聞くあいだ、読み取り結果を預かる（AIをもう一度呼ばないため）。"""
    _save_analyzed(jid, a)
    store.update_user(c.uid, {"sale_setup_job": jid, "sale_setup_at": store.now(),
                              "reg_queue": ["assoc", "sfee"], "reg_mode": "sale_setup"})
    c.refresh()
    c.send(texts.build("sale_need_setup"), texts.build("sale_reg_assoc"))


def resume_after_setup(c):
    """2つの質問が終わったら、預かった図面で作る（宛名を聞いていれば、その宛名で）。"""
    jid = c.u.get("sale_setup_job")
    at = c.u.get("sale_setup_at")
    store.update_user(c.uid, {"sale_setup_job": None, "sale_setup_at": None, "reg_mode": None})
    c.refresh()
    job = store.get_job(jid) if jid else None
    if not job or job.get("status") != "analyzed" or (at and at < store.now() - timedelta(hours=24)):
        return c.send(line.text("売買の設定ができました。お預かりしていた販売図面の期限が切れたため、もう一度送ってください。"))
    a = _load_analyzed(jid, job)
    if ask_next(c, jid, a, job.get("宛名") or "", saved=True):
        return
    line.loading(c.line_uid, 60)
    return finish(c, jid, a, job.get("宛名") or "")


FONT_DIR = str(__import__("pathlib").Path(__file__).parent / "fonts" / "noto-sans-jp-ttf")


def estimate_html(company, evals=None, loan_override=None, calib=None, handover=None, drawing_day=None, teiren=None):
    """購入諸費用の概算書（A4 1枚のHTML）を作る関数を返す。sale_core.render_documents の estimate_html に渡す。
    evals はスタッフが送った評価額 {"land_eval", "bldg_eval", ...}（なければ価格から推計）。
    スタッフ向けの確認事項は、返した関数の .notes に入る（お客様向けPDFには出さず、社内向けの注意へ）。"""
    try:
        import sale_estimate
    except ImportError:
        return None
    import sale_core

    def build(result, co):
        params = inspect.signature(sale_estimate.make_estimate).parameters
        kw = {}
        if "teiren" in params:  # この物件だけの仲介手数料の扱い（800万円以下の特例）は計算側が受ける
            kw["teiren"] = teiren or None
        else:
            co = apply_teiren(co, teiren)
        f = result.事実
        # co は sale_core.render_documents の中で band_company 済み（免許の行に所属団体が入っている）
        band = {"name": co.get("会社名", ""), "license": co.get("免許", ""),
                "address": co.get("住所", ""), "tel": co.get("電話", ""), "staff": co.get("担当者名", ""),
                "mobile": co.get("携帯", "") if co.get("携帯を載せる", True) else ""}
        name = " ".join(x for x in (f.物件名, f.号室 and f"{f.号室}号室") if x) or f.所在地 or ""
        # 会社の設定（company["売買ローン目安"]）と物件ごとの指定（loan_override）は、そのまま概算書側に渡す。
        # 重ね方（物件ごとの金利は今のタイプに入れる、借入額の指定は fixed にする等）は sale_estimate.loan_conditions が持つ
        facts = sale_core.cost_inputs(f)
        if facts and facts.get("address"):
            facts["land_calib"] = (calib or calib_factor)(facts["address"])
        # 決済日（引渡日）：スタッフが8桁で送った日があればそれ（確定）。なければ図面を送った日の翌々月1日（仮）
        if handover:
            kw["handover_day"] = handover
        if not handover and "drawing_day" in params:  # 仮の決済日の決め方は計算側が持つ（図面を送った日を渡すだけ）
            kw["drawing_day"] = drawing_day or today_jst()
        out = sale_estimate.make_estimate(co, facts, evals, customer=co.get("宛名") or "",
                                          property_name=name, band=band, font_dir=FONT_DIR,
                                          loan_override=loan_override or None, **kw)
        build.notes[:] = list(out.get("staff_notes") or [])
        build.teiren_judge = out.get("teiren_judge")
        return out["html"]
    build.notes = []
    build.teiren_judge = None
    return build


# ---------- 土地の評価額の推計を、実績で地域ごとに補正する（calibration） ---------------------------
# スタッフが実際の評価額を送るたびに1件記録し（Firestore の sale_calibration、資料ごとに1件）、
# 同じ町に3件・同じ市区町村に5件たまると、次の推計から SaleFacts.land_calib に係数が入る。

def calib_factor(address):
    try:
        import calibration
        import evaluation
        city, town, _ = evaluation.split_address(address or "")
        if not city:
            return 1.0
        return calibration.factor(store.calib_records(city), city, town)[0]
    except Exception:
        log.exception("calib_factor")
        return 1.0


def record_calibration(jid, result, land_eval):
    """実際の土地の評価額と、補正なし（係数1.0）の推計単価を比べた記録を残す。面積が分からないときは残さない。"""
    try:
        import calibration
        import costs_sale
        import evaluation
        import sale_core
        from datetime import date
        facts = sale_core.cost_inputs(result.事実)
        if not facts or not facts.get("address"):
            return None
        f = costs_sale.SaleFacts(**{**facts, "land_calib": 1.0, "land_eval": None})
        evi = costs_sale.estimate_evals(f, date.today() + timedelta(days=60))
        area = f.land_area
        city, town, _ = evaluation.split_address(f.address)
        rec = calibration.make_record(city, town, evi.land_unit_est, land_eval, area, store.now().date().isoformat())
        if rec:
            store.add_calib_record(jid, rec)
        return rec
    except Exception:
        log.exception("record_calibration")
        return None


# ---------- 図面を送った直後に、住宅ローンの予定を聞く（マンション・戸建てだけ。土地は聞かない） -----------
# 答えは job.loan（物件ごとの指定）に入れて概算書へ。作り直し（「ローン条件を変える」）は件数に数えない

def is_land(result):
    return getattr(result.事実, "物件種別", "") == "土地"


def _land_staff():
    try:
        from costs_sale import LAND_LOAN_STAFF
        return LAND_LOAN_STAFF
    except ImportError:
        return ""


def ask_loan(c, jid, a, atena="", saved=False):
    """聞いたら True（答えが届いたら finish）。会社設定で「聞かない」・土地のときは False。"""
    L = loan(c.u.get("company") or {})
    if is_land(a["result"]) or not L.get("ask_loan", True):
        return False
    if not saved:
        _save_analyzed(jid, a)
    store.update_job(jid, {"宛名": atena, "loan": None})
    store.update_user(c.uid, {"sale_lq": {"jid": jid, "step": "q1", "at": store.now()}})
    c.send(texts.build("sale_loan_q1", 借入の目安=amount_label(L)))
    return True


def _lq_job(c):
    q = c.u.get("sale_lq") or {}
    jid = q.get("jid")
    job = store.get_job(jid) if jid else None
    if not job or job.get("status") != "analyzed" or job.get("user_id") != c.uid or \
            (q.get("at") and q["at"] < store.now() - timedelta(hours=24)):
        return None, None
    return jid, job


def _lq_q2(c, jid, lo):
    store.update_job(jid, {"loan": lo})
    store.update_user(c.uid, {"sale_lq": {**(c.u.get("sale_lq") or {}), "step": "q2"}})
    L = loan(c.u.get("company") or {})
    now = "3タイプで比べる" if L.get("show") and L.get("compare") else "付ける" if L.get("show") else "付けない"
    return c.send(texts.build("sale_loan_q2", いつもの設定=now))


def _lq_finish(c, jid, job, lo):
    store.update_job(jid, {"loan": lo})
    store.update_user(c.uid, {"sale_lq": None})
    c.refresh()
    a = _load_analyzed(jid, store.get_job(jid))
    if ask_next(c, jid, a, job.get("宛名") or "", saved=True, after="loan"):  # 800万円以下の手数料・評価額の出し方
        return
    line.loading(c.line_uid, 60)
    return finish(c, jid, a, job.get("宛名") or "")


def lq_postback(c, k, v):
    """lq=yes/no/amount/unknown（Q1）、lq2=show/cmp/off（Q2）。"""
    jid, job = _lq_job(c)
    if not jid:
        store.update_user(c.uid, {"sale_lq": None})
        return c.send(line.text("お預かりしていた販売図面が見つかりませんでした。お手数ですが、もう一度送ってください。"))
    if k == "lq":
        if v == "no":
            return _lq_finish(c, jid, job, {"use": False})
        if v == "amount":
            store.update_user(c.uid, {"sale_lq": {**c.u["sale_lq"], "step": "amount"}})
            return c.send(texts.build("sale_loan_amount_ask"))
        return _lq_q2(c, jid, {"use": True})
    lo = {**(job.get("loan") or {"use": True})}
    lo.update({"show": {"show": True, "cmp": True}.get(v, False), **({"compare": v == "cmp"} if v in ("show", "cmp") else {})})
    return _lq_finish(c, jid, job, lo)


def lq_text(c, t):
    """借入額の入力（「3500万」など）。扱ったら True。"""
    q = c.u.get("sale_lq") or {}
    if q.get("step") != "amount":
        return False
    s = unicodedata.normalize("NFKC", t or "").replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(億|万)?", s)
    amt = int(round(float(m.group(1)) * {"億": 100_000_000, "万": 10_000}.get(m.group(2), 1))) if m else 0
    if amt < 1_000_000 or amt > 2_000_000_000:  # 100万円〜20億円だけ受け付ける（「3500」は万円とみなす）
        amt = amt * 10_000 if m and not m.group(2) and 100 <= amt <= 200_000 else 0
    if not amt:
        c.send(texts.build("sale_loan_amount_ask"))
        return True
    jid, job = _lq_job(c)
    if not jid:
        store.update_user(c.uid, {"sale_lq": None})
        return False
    return _lq_q2(c, jid, {"use": True, "amount": amt}) or True


def finish(c, jid, a, atena="", after=()):
    """売買の資料を作り、件数を1つ使って届ける（賃貸版の bot.finish と同じ流れ）。"""
    import bot
    import sale_core
    consumed = False
    try:
        if not store.try_consume(c.uid):
            store.update_job(jid, {"status": "failed", "error": "quota"})
            q = store.quota(store.get_user(c.uid))
            mid = {"light": "plan_light_empty", "unlimited": "plan_max_empty"}.get(q["plan"], "plan_needed")
            return c.send(texts.build(mid, urls=bot.plan_urls(c), 更新日=store.renew_date(c.u), **bot.plan_kw()), *after)
        consumed = True
        company = {**bot.company_for_render(c.uid, c.u), "宛名": atena}
        job0 = store.get_job(jid) or {}
        # 図面の直後に聞いたローン・800万円以下の手数料の答え（なければ会社設定）
        te = job0.get("teiren") or ({"mode": "tokurei"} if teiren_setting(company) == "tokurei" else None)  # 「いつも特例」は上限33万円で
        est = estimate_html(company, job0.get("evals") or None, loan_override=job0.get("loan"), teiren=te)  # 評価額（路線価の返信など）
        if is_land(a["result"]):
            after = (texts.build("sale_land_loan_note", スタッフ向けの案内例=_land_staff()),) + tuple(after)
        out = sale_core.render_documents(company, a["img"], a["result"], a["pages"], a["log"], estimate_html=est)
        if est is not None:  # 概算書のスタッフ向け確認事項（評価額は推計、など）を社内向けの注意に足す
            out["texts"]["chui"] = list(out["texts"].get("chui") or []) + est.notes
        store.save_files(jid, out)
        store.update_job(jid, {"status": "done", "kind": "sale", "title": out["title"], "texts": out["texts"],
                               "log": out["log"], "pages": out["pages"], "result": out["result"], "宛名": atena,
                               "verdict": "sale", "teiren_check": _judge(est, a["result"])})
        recent = [{"id": jid, "title": out["title"], "at": store.now(), "kind": "sale"}] + \
                 [r for r in (c.u.get("recent_jobs") or []) if r["id"] != jid]
        store.update_user(c.uid, {"last_job": jid, "pending": None, "recent_jobs": recent[:12]})
        c.refresh()
        deliver(c, jid, after=after)
    except Exception as e:
        log.exception("売買の資料づくりで例外")
        if consumed:
            store.refund(c.uid)
        store.update_job(jid, {"status": "failed", "error": repr(e)[:500]})
        c.send(texts.build("err_system"), *after)


KEY_NOTES = re.compile(r"告知事項|物件確認|再建築|ページありました")  # 案内用の資料なので、広告の可否は要点に出さない。複数ページのPDFは1ページ目で作った旨をLINEにも出す


def key_notes(job):
    """LINEの文に入れる大事な注意（最大2行）。"""
    chui = [x for x in (job.get("texts") or {}).get("chui", []) if KEY_NOTES.search(x)]
    return "".join(f"\n【ご注意】{x}" for x in chui[:2]) + ("\n" if chui else "")


def osusume_message(job):
    """物件のおすすめポイント（お客様へ送る文章）。(案内の一文, 転送用の吹き出し or None)。
    スタッフ向けの一言（規約3条2項と同じ「お客様へ送る前に内容をご確認ください」）は、吹き出しのすぐ上の案内に入れ、
    転送する吹き出しには入れない。強い言葉などで自動で外れて空のときは、吹き出しを送らない。"""
    t = job.get("texts") or {}
    pts = [p for p in (t.get("osusume") or []) if (p.get("見出し") or p.get("説明"))]
    note = t.get("osusume_note") or "お客様へ送る前に内容をご確認ください"
    if not pts:
        return "\n▼物件のおすすめポイント\n今回は自動ではお作りしていません。社内向けPDFの「スタッフへの注意」をご覧ください。\n", None
    body = f"【{job.get('title') or '物件'}】のおすすめポイント\n\n" + "\n\n".join(
        f"■{p.get('見出し', '')}\n{p.get('説明', '')}".strip() for p in pts)
    return f"\n▼物件のおすすめポイント（次のメッセージ）\n長押しして「転送」で、お客様に送れます。{note}。\n", line.text(body[:4900])


def deliver(c, jid, after=()):
    import bot
    job = store.get_job(jid)
    d = c.u.get("delivery") or {}
    exp = job["expires_at"].astimezone(store.JST)
    url_okyaku, url_shanai = store.pdf_urls(jid, job)
    img = line.image(store.file_url(jid, job, "obikae.jpg"), store.file_url(jid, job, "preview.jpg"))
    drop = () if ((job.get("texts") or {}).get("motozuke") or {}).get("fax") else ("postback bfax=from_last&doc={資料ID}",)
    if not job.get("teiren_check"):  # 「手数料を変える」は800万円以下（かもしれない）物件だけ
        drop += ("postback tq=ask&doc={資料ID}",)
    o_note, o_msg = osusume_message(job)
    common = dict(物件名=job["title"], のこり枚数の案内=bot.remain_note(c.u), 資料ID=jid, 注意の要点=key_notes(job),
                  おすすめの案内=o_note, drop=drop)
    after = ((o_msg,) if o_msg else ()) + tuple(after)
    if d.get("method") == "email" and d.get("email"):
        import delivery
        if delivery.send_mail(d["email"], job["title"], store.pdf_files(jid, job, job["title"]),
                              store.file_url(jid, job, "shiryou.pdf")):
            return c.send(img, *_qr_last(texts.build("sale_deliver_mail", メールアドレス=d["email"], **common), after))
    c.send(img, *_qr_last(texts.build("sale_deliver_line", お客様向けリンク=url_okyaku, 社内向けリンク=url_shanai,
                                      有効期限=f"{exp.month}月{exp.day}日", **common), after))


def _qr_last(msg, after):
    """LINEのボタン（クイックリプライ）は最後の吹き出しにしか出ないので、後ろに吹き出しが続くときは最後へ移す。
    転送してもクイックリプライは付いていかない。"""
    after = [dict(m) for m in after]
    if after and msg.get("quickReply") and not after[-1].get("quickReply") and after[-1].get("type") == "text":
        after[-1]["quickReply"] = msg.pop("quickReply")
    return [msg, *after]


# ---------- 元付へのFAX（振り分け） --------------------------------------------------------

def fax_start(c):
    if SALE_ONLY:  # 売買版のLINE：元付へのFAXは、売買の内見依頼書だけ
        return c.send(texts.build("sale_fax_start")) or True
    kinds = {r.get("kind", "rent") for r in (c.u.get("recent_jobs") or [])}
    if kinds == {"sale"}:
        return c.send(texts.build("sale_fax_start"))
    if "sale" in kinds:
        return c.send(texts.build("sale_fax_choose"))
    return None  # 賃貸だけ → 今までどおり内見申込書へ


def bfax_postback(c, v):
    """bfax=start（案内）／bfax=recent（最近の売買の物件から選ぶ）／bfax=from_last&doc=資料ID（その物件で作る）。"""
    import naiken_bot
    if v.startswith("from_last"):
        jid = v.partition("doc=")[2] or c.u.get("last_job")
        return naiken_bot.start_from_job(c, jid)
    if v == "recent":
        since = store.now() - timedelta(days=7)
        rec = [r for r in (c.u.get("recent_jobs") or []) if r.get("kind") == "sale" and (not r.get("at") or r["at"] >= since)][:12]
        if not rec:
            return c.send(line.text("最近お作りした売買の物件がありません。売買の販売図面を送ってください。",
                                    quick=[{"type": "action", "action": {"type": "camera", "label": "販売図面を送る"}}]))
        q = [line.qr_postback(naiken_bot.short_label(r["title"]), f"bfax=from_last&doc={r['id']}") for r in rec]
        return c.send(line.text("どの物件の内見依頼書を作りますか？（最近7日の売買の物件）", quick=q))
    return c.send(texts.build("sale_fax_start"))


# ---------- 賃貸版の登録内容を引き継ぐ（売買版のLINEで友だち追加されたとき） -------------------
# 帯・名刺の会社情報だけ写す。料金・件数・資料・FAXの履歴は写さない（賃貸版と売買版は別のサービス）
RENT_ONLY_KEYS = {"賃貸_仲介手数料_ヶ月"}


def rental_company(c):
    r = store.rental_user(c.line_uid)
    co = (r or {}).get("company") or {}
    return co if (r or {}).get("registered") and co.get("会社名") else None


def copy_from_rental(c):
    """賃貸版の会社情報を写して、売買だけの2問（所属団体・売買の仲介手数料）を聞く。写せたら True。"""
    co = rental_company(c)
    if not co:
        return False
    keep = {k: v for k, v in (c.u.get("company") or {}).items() if k in ("所属団体", "所属団体_種類", "売買設定", "売買ローン目安")}
    store.update_user(c.uid, {"company": {**{k: v for k, v in co.items() if k not in RENT_ONLY_KEYS}, **keep},
                              "use_kind": "sale", "copied_from_rent_at": store.now()})
    c.refresh()
    return True


# ---------- 登録内容の変更（売買の設定） ----------------------------------------------------

EDIT = {"sfee": ("売買の仲介手数料", ["sfee"]), "assoc": ("所属団体", ["assoc"])}


def edit_postback(c, k, v):
    """「賃貸・売買の設定」以下のボタン。扱ったら True。"""
    co = c.u.get("company") or {}
    if k == "edit" and v == "deal":
        # 売買版のLINE（SALE_ONLY）では賃貸の仲介手数料は出さない
        c.send(texts.build("sale_edit_menu", drop=("postback edit=fee",) if SALE_ONLY else ()))
        return True
    if k == "edit" and v == "nqr":
        import baibai_naiken
        c.send(texts.build("sale_edit_nqr", 今の設定="載せる" if naiken_qr_on(co) else "載せない", 一言=baibai_naiken.QR_CAPTION))
        return True
    if k == "nqr" and v in ("on", "off"):
        set_company(c, 内見依頼書のQR=v == "on")
        c.send(texts.build("sale_edit_done", 項目="内見依頼書のQRコード"))
        return True
    if k == "edit" and v == "tokurei":
        c.send(texts.build("sale_edit_tokurei", 今の設定=TEIREN_SETTING_NAMES[teiren_setting(co)]))
        return True
    if k == "tokurei" and v in ("ask", "on", "off"):
        m = {"ask": "ask", "on": "tokurei", "off": "normal"}[v]
        # 聞かなかったとき（答えがない・続けて送られた）に使う値も合わせる。「毎回聞く」は通常で作る
        set_settings(c, teiren_ask=m, teiren_tokurei=m == "tokurei")
        c.send(texts.build("sale_edit_done", 項目=f"800万円以下の物件の手数料（{TEIREN_SETTING_NAMES[m]}）"))
        return True
    if k == "edit" and v == "evalask":
        c.send(texts.build("sale_edit_evalask", 今の設定=EVAL_ASK_NAMES[eval_setting(co)]))
        return True
    if k == "evalask" and v in EVAL_ASK_NAMES:
        set_settings(c, eval_ask=v)
        c.send(texts.build("sale_edit_done", 項目=f"土地の評価額の出し方（{EVAL_ASK_NAMES[v]}）"))
        return True
    if k == "edit" and v == "shiho":
        s = settings(co)
        store.update_user(c.uid, {"sale_edit": "shiho"})
        c.send(texts.build("sale_edit_shiho", 移転=f"{s['shihoshoshi_transfer']:,}", 抵当権=f"{s['shihoshoshi_mortgage']:,}",
                           保存=f"{s['shihoshoshi_preserve']:,}", 実費=f"{s['touki_jippi']:,}"))
        return True
    if k == "edit" and v == "loan":
        return loan_menu(c)
    if k == "loan":
        return loan_postback(c, v)
    return False


def naiken_qr_on(co):
    """内見依頼書の下に、1分資料のQR（HPの売買ページへ）と一言を載せるか（会社の設定。初期値は載せる）。"""
    return (co or {}).get("内見依頼書のQR", True) is not False


# ---------- 住宅ローンシミュレーション（会社の設定） ------------------------------------------

def _save_loan(c, **kv):
    L = loan(c.u.get("company") or {})
    L.update(kv)
    L.pop("rate", None)
    set_company(c, 売買ローン目安=L)


def loan_menu(c, prefix=None):
    L = loan(c.u.get("company") or {})
    store.update_user(c.uid, {"sale_edit": "loan"})
    r = L["rates"]
    msg = texts.build("sale_edit_loan", 付ける="付ける" if L["show"] else "付けない", 条件=loan_label(L),
                      変動=f"{r['hendo']:g}", 固定期間=f"{r['kotei_kikan']:g}", 固定期間の年数=str(L["kotei_years"]),
                      全期間固定=f"{r['zenki_kotei']:g}", 借入額=amount_label(L),
                      比較="3タイプを並べる" if L.get("compare") else "選んだタイプだけ",
                      毎回聞く="毎回聞く" if L.get("ask_loan", True) else "聞かない（この設定で作る）",
                      drop=("postback loan=on" if L["show"] else "postback loan=off",
                            "postback loan=cmp_on" if L.get("compare") else "postback loan=cmp_off",
                            "postback loan=ask_on" if L.get("ask_loan", True) else "postback loan=ask_off"))
    return c.send(*([prefix] if prefix else []), msg) or True


def loan_postback(c, v):
    if v in ("on", "off"):
        _save_loan(c, show=v == "on")
        return loan_menu(c, texts.build("sale_edit_done", 項目="住宅ローンシミュレーション（" + ("付ける" if v == "on" else "付けない") + "）"))
    if v in ("ask_on", "ask_off"):
        _save_loan(c, ask_loan=v == "ask_on")
        return loan_menu(c, texts.build("sale_edit_done", 項目="図面を送った後のローンの質問（" + ("毎回聞く" if v == "ask_on" else "聞かない") + "）"))
    if v in ("cmp_on", "cmp_off"):
        _save_loan(c, compare=v == "cmp_on")
        return loan_menu(c, texts.build("sale_edit_done", 項目="住宅ローンシミュレーション（" + ("3タイプを並べる" if v == "cmp_on" else "1つだけ載せる") + "）"))
    if v == "type":
        c.send(texts.build("sale_loan_type"))
        return True
    if v in LOAN_TYPES:
        _save_loan(c, type=v)
        return loan_menu(c, texts.build("sale_edit_done", 項目=f"金利のタイプ（{LOAN_TYPES[v]}）"))
    if v == "amount":
        c.send(texts.build("sale_loan_amount"))
        return True
    if v in ("full", "plus_costs", "r90"):
        _save_loan(c, amount_mode="price_plus_costs" if v == "plus_costs" else "ratio", ratio=0.9 if v == "r90" else 1.0)
        return loan_menu(c, texts.build("sale_edit_done", 項目="借入額の決め方"))
    return False


LOAN_TYPE_WORDS = [("zenki_kotei", r"全期間固定|フラット"), ("kotei_kikan", r"固定期間|固定\s*\d+\s*年|固定(?!.*全期間)"),
                   ("hendo", r"変動")]


def parse_loan(s):
    """「借入 3500万 変動 1.2% 35年」「固定10年 1.6」「ローンなし」などを読む。読めた項目だけ返す。"""
    s = unicodedata.normalize("NFKC", s or "").replace(",", "")
    out = {}
    if re.search(r"ローン(は)?(なし|無し|使わない)|現金", s):
        return {"use": False}  # ローン関連の費用も外す（show:False だとシミュレーション欄だけ消える）
    m = re.search(r"借入(額)?\D{0,4}?(\d+(?:\.\d+)?)\s*(億|万)?", s)
    if m:
        out["amount"] = int(round(float(m.group(2)) * {"億": 100_000_000, "万": 10_000}.get(m.group(3), 1)))
    for key, pat in LOAN_TYPE_WORDS:
        if re.search(pat, s):
            out["type"] = key
            break
    m = re.search(r"固定\s*(\d+)\s*年", s)
    if m and out.get("type") == "kotei_kikan":
        out["kotei_years"] = int(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", s) or re.search(r"金利\D{0,3}?(\d+(?:\.\d+)?)", s)
    if not m and out.get("type"):  # 「全期間固定 2.0」のように%がないときは、タイプの後ろの小さい数字を金利とみる
        rest = re.sub(r"固定\s*\d+\s*年", "", s)
        m = re.search(r"(?<![\d.])(\d{1,2}(?:\.\d+)?)(?![\d.]|\s*(年|万|億|円))", rest)
    if m and 0 < float(m.group(1)) < 20:
        out["rate"] = float(m.group(1))
    s2 = re.sub(r"固定\s*\d+\s*年", "", s)
    m = re.search(r"(\d+)\s*年(?!\s*目)", s2) or re.search(r"年数\D{0,3}?(\d+)", s2)
    if m and 1 <= int(m.group(1)) <= 50:
        out["years"] = int(m.group(1))
    return out


def loan_settings_text(c, s):
    """会社の設定：「変動 1.2」「固定10年 3.7」「全期間固定 3.6」「年数 35」「割合 90」。"""
    L = loan(c.u.get("company") or {})
    rates = dict(L["rates"])
    hit = False
    for key, pat in LOAN_TYPE_WORDS:
        m = re.search(f"(?:{pat})[^\\d\\n]{{0,6}}?(\\d+(?:\\.\\d+)?)(?![\\d.])[ \\t]*%?(?![ \\t]*年)", s)
        if m and 0 < float(m.group(1)) < 20:
            rates[key] = float(m.group(1))
            hit = True
    kv = {"rates": rates}
    m = re.search(r"固定\s*(\d+)\s*年", s)
    if m:
        kv["kotei_years"] = int(m.group(1)); hit = True
    m = re.search(r"年数\D{0,3}?(\d+)", s) or re.search(r"(?<!固定)(?<!\d)(\d{2})\s*年", re.sub(r"固定\s*\d+\s*年", "", s))
    if m and 1 <= int(m.group(1)) <= 50:
        kv["years"] = int(m.group(1)); hit = True
    m = re.search(r"割合\D{0,3}?(\d+(?:\.\d+)?)", s)
    if m and 10 <= float(m.group(1)) <= 100:
        kv.update(amount_mode="ratio", ratio=float(m.group(1)) / 100); hit = True
    if not hit:
        return False
    _save_loan(c, **kv)
    store.update_user(c.uid, {"sale_edit": None})
    loan_menu(c, texts.build("sale_edit_done", 項目="住宅ローンシミュレーションの条件"))
    store.update_user(c.uid, {"sale_edit": "loan"})
    return True


SHIHO_KEYS = {"移転": "shihoshoshi_transfer", "抵当権": "shihoshoshi_mortgage", "保存": "shihoshoshi_preserve",
              "実費": "touki_jippi"}


def edit_text(c, t):
    """司法書士報酬・返済の目安の数字。扱ったら True。"""
    mode = c.u.get("sale_edit")
    if not mode:
        return False
    s = unicodedata.normalize("NFKC", t or "").replace(",", "")
    if mode == "eval":
        return eval_text(c, s)
    if mode == "shiho":
        kv = {SHIHO_KEYS[k]: int(n) for k, n in re.findall(r"(移転|抵当権|保存|実費)\D*?(\d+)", s) if int(n) <= 1_000_000}
        if not kv:
            return False
        set_settings(c, **kv)
    elif mode == "loan":
        return loan_settings_text(c, s)
    elif mode == "lsim":
        return lsim_text(c, s)
    elif mode == "hand":
        return hand_text(c, s)
    store.update_user(c.uid, {"sale_edit": None})
    c.send(texts.build("sale_edit_done", 項目="司法書士報酬の目安"))
    return True


def monthly_payment(principal, rate_pct, years):
    """元利均等の月々の返済額（円、1円未満切り上げ）。"""
    n = years * 12
    r = rate_pct / 100 / 12
    if r == 0:
        return -(-principal // n)
    p = principal * r * (1 + r) ** n / ((1 + r) ** n - 1)
    return int(-(-p // 1))


# ---------- 評価額を送って作り直す（AIは使わない・件数に数えない） -------------------------------

def parse_evals(s):
    s = unicodedata.normalize("NFKC", s or "").replace(",", "").replace("円", "")
    out = {}
    for key, name in (("land_eval", "土地"), ("bldg_eval", "建物")):
        m = re.search(name + r"\D{0,6}?(\d+(?:\.\d+)?)\s*(万)?", s)
        if m:
            out[key] = int(round(float(m.group(1)) * (10_000 if m.group(2) else 1)))
    # 固定資産税・都市計画税の年税額（元付業者から聞いた値を送ってもらう）
    m = re.search(r"(?:年税額?|固都税|税額)\D{0,6}?(\d+(?:\.\d+)?)\s*(万)?", s)
    if m:
        out["annual_property_tax"] = int(round(float(m.group(1)) * (10_000 if m.group(2) else 1)))
    # 相続税路線価（路線価図の道路の数字。「路線価 320」「路線価320D」）
    m = re.search(r"路線価\D{0,4}?(\d+(?:\.\d+)?\s*万?[A-GＡ-Ｇa-g]?)", s)
    if m:
        try:
            import evaluation
            v = evaluation.parse_rosenka_reply(m.group(1))
        except ImportError:
            v = None
        if v:
            out["rosenka_per_m2"] = int(v)
    return out


def rerender(c, jid, evals=None, loan_override=None, handover=None, teiren=None):
    """預かっている図面と読み取り結果で、概算書を作り直す（AIは使わない・件数に数えない）。
    これまでに送られた評価額・ローンの条件は残し、新しく送られた方で上書きする。お客様向けPDFのURLを返す（できなければ None）。"""
    import bot
    import sale_core
    import sale_schema
    from PIL import Image
    job = store.get_job(jid) if jid else None
    page = store.get_page(jid) if job else None
    if not job or job.get("user_id") != c.uid or job.get("kind") != "sale" or not page:
        return None
    ev = {**(job.get("evals") or {}), **(evals or {})}
    lo = {**(job.get("loan") or {}), **(loan_override or {})} or None
    if (job.get("loan") or {}).get("use") is False and loan_override and "use" not in loan_override \
            and set(loan_override) - {"show", "compare"}:
        lo["use"] = True  # 「ローンなし」の後に条件を送られたら、ローンを使う前提に戻す
    hd = handover if handover is not None else job.get("handover")  # "" は仮に戻す
    hd = date.fromisoformat(hd) if hd else None
    te = teiren if teiren is not None else job.get("teiren") or \
        ({"mode": "tokurei"} if teiren_setting(c.u.get("company") or {}) == "tokurei" else None)
    est = estimate_html(c.u.get("company") or {}, ev or None, lo, handover=hd, drawing_day=job_day(job), teiren=te)
    if est is None:
        return None
    company = {**bot.company_for_render(c.uid, c.u), "宛名": job.get("宛名") or ""}
    r = sale_schema.SaleResult.model_validate(job["result"])
    out = sale_core.render_documents(company, Image.open(io.BytesIO(page)).convert("RGB"), r,
                                     job.get("pages") or 0, job.get("log") or {}, estimate_html=est)
    store.save_files(jid, out)
    store.update_job(jid, {"evals": ev, "loan": lo, "handover": hd.isoformat() if hd else None, "teiren": te})
    if evals and evals.get("land_eval"):
        record_calibration(jid, r, int(evals["land_eval"]))
    return store.pdf_urls(jid, store.get_job(jid))[0]


def eval_text(c, s):
    ev = parse_evals(s)
    if not ev:
        return False
    jid = c.u.get("sale_eval_job")
    store.update_user(c.uid, {"sale_edit": None, "sale_eval_job": None})
    url = rerender(c, jid, evals=ev)
    if not url:
        c.send(line.text("申し訳ありません。この資料は作り直せませんでした。もう一度、販売図面から送ってください。"))
        return True
    c.send(texts.build("sale_eval_done", お客様向けリンク=url))
    return True


# ---------- 物件ごとのローン条件（この資料だけ作り直す） ----------------------------------------

def lsim_ask(c, jid):
    store.update_user(c.uid, {"sale_edit": "lsim", "sale_eval_job": jid})
    L = loan(c.u.get("company") or {})
    job = store.get_job(jid) or {}
    cur = job.get("loan") or {}
    now = "住宅ローンを使わない前提（ローンの費用なし）" if cur.get("use") is False else "シミュレーションは載せていません" if cur.get("show") is False else (
        loan_label({**L, **{k: v for k, v in cur.items() if v}}) + (f"・借入{cur['amount'] // 10_000:,}万円" if cur.get("amount") else f"・借入は{amount_label(L)}"))
    return c.send(texts.build("sale_lsim_ask", 今の条件=now))


def lsim_text(c, s):
    lo = parse_loan(s)
    if not lo:
        return False
    jid = c.u.get("sale_eval_job")
    store.update_user(c.uid, {"sale_edit": None, "sale_eval_job": None})
    url = rerender(c, jid, loan_override=lo)
    if not url:
        c.send(line.text("申し訳ありません。この資料は作り直せませんでした。もう一度、販売図面から送ってください。"))
        return True
    c.send(texts.build("sale_lsim_done", お客様向けリンク=url))
    return True


# ---------- 決済日（引渡日）：この資料だけ変えて作り直す ----------------------------------------

def today_jst():
    return store.now().astimezone(store.JST).date()


def job_day(job):
    """図面を送った日（日本時間）。"""
    t = (job or {}).get("created_at")
    return t.astimezone(store.JST).date() if hasattr(t, "astimezone") else today_jst()


def tentative_handover(d):
    """図面を送った日の翌々月1日（仮の決済日）。決め方は計算側（costs_sale.tentative_handover）を唯一の元にする。"""
    try:
        from costs_sale import tentative_handover as th
        return th(d)
    except ImportError:
        return date(d.year + (d.month + 1) // 12, (d.month + 1) % 12 + 1, 1)


def parse_day(s, today=None):
    """「20270301」「2027/3/1」「2027年3月1日」を日付に。今日より前・2年より先は受け付けない。"""
    s = unicodedata.normalize("NFKC", s or "")
    m = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", s) or \
        re.search(r"(20\d{2})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})", s)
    if not m:
        return None
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    today = today or today_jst()
    return d if today <= d <= today + timedelta(days=730) else None


def day_label(d):
    return f"{d.year}年{d.month}月{d.day}日（{'月火水木金土日'[d.weekday()]}）"


def hand_ask(c, jid):
    store.update_user(c.uid, {"sale_edit": "hand", "sale_eval_job": jid})
    job = store.get_job(jid) or {}
    now = day_label(date.fromisoformat(job["handover"])) if job.get("handover") else \
        day_label(tentative_handover(job_day(job))) + "（仮：図面を送った日の翌々月1日）"
    return c.send(texts.build("sale_hand_ask", 今の決済日=now, drop=() if job.get("handover") else ("postback hand=reset",)))


def hand_text(c, s):
    d = parse_day(s)
    if not d:
        if re.search(r"\d{6,}|年|/", s):
            c.send(texts.build("sale_hand_bad"))
            return True
        return False
    return _hand_rerender(c, d.isoformat(), day_label(d))


def hand_reset(c, jid):
    store.update_user(c.uid, {"sale_eval_job": jid})
    job = store.get_job(jid) or {}
    return _hand_rerender(c, "", day_label(tentative_handover(job_day(job))) + "（仮）")


def _hand_rerender(c, iso, label):
    jid = c.u.get("sale_eval_job")
    store.update_user(c.uid, {"sale_edit": None, "sale_eval_job": None})
    url = rerender(c, jid, handover=iso)
    if not url:
        c.send(line.text("申し訳ありません。この資料は作り直せませんでした。もう一度、販売図面から送ってください。"))
        return True
    c.send(texts.build("sale_hand_done", 決済日=label, お客様向けリンク=url))
    return True


# ---------- 800万円以下の物件の仲介手数料（低廉な空家等の特例・上限33万円） ---------------------------
# 図面の価格（税抜の本体価格）が800万円以下なら、ローンの質問のあとに1問だけ聞く（会社設定で「毎回聞く」のとき）。
# 答えは job.teiren = {"mode": "normal"|"tokurei"|"amount", "yen": 金額（amount のとき）}。
# 作り直し（届いた資料の「手数料を変える」）は件数に数えない。

TEIREN_LIMIT = 8_000_000
TEIREN_MAX = 330_000  # 税込
TEIREN_SETTING_NAMES = {"ask": "毎回聞く", "normal": "いつも通常の上限で計算", "tokurei": "いつも特例（上限33万円）で計算"}


def teiren_setting(co):
    raw = (co or {}).get("売買設定") or {}
    if raw.get("teiren_ask") in TEIREN_SETTING_NAMES:
        return raw["teiren_ask"]
    return "tokurei" if raw.get("teiren_tokurei") else "ask"  # 以前の「使う」の設定は「いつも特例」として読む


def teiren_check(result):
    """"yes"＝税抜の本体価格が800万円以下、"maybe"＝税込の表示で建物の消費税が分からず800万円前後、None＝対象外。"""
    import sale_core
    try:
        fx = sale_core.cost_inputs(result.事実)
    except Exception:
        log.exception("800万円以下の判定に失敗")
        return None
    if not fx or not fx.get("price"):
        return None
    price, tax = int(fx["price"]), fx.get("price_tax")
    if price - int(tax or 0) <= TEIREN_LIMIT:
        return "yes"
    if not tax and fx.get("property_type") != "land" and fx.get("seller_is_business") is not False \
            and price <= TEIREN_LIMIT * 1.1:
        return "maybe"
    return None


def apply_teiren(co, t):
    """この物件だけの手数料の扱いを、概算書に渡す会社設定（売買設定）に重ねる。"""
    if not t or t.get("mode") not in ("normal", "tokurei", "amount"):
        return co
    s = dict((co or {}).get("売買設定") or {})
    if t["mode"] == "normal":
        s["teiren_tokurei"] = False
    elif t["mode"] == "tokurei":
        s.update(teiren_tokurei=True, fee_mode="max")
    else:
        s.update(teiren_tokurei=True, fee_mode="custom", fee_custom=min(int(t.get("yen") or 0), TEIREN_MAX))
    return {**(co or {}), "売買設定": s}


def teiren_label(t):
    if not t:
        return "会社の設定どおり"
    if t.get("mode") == "tokurei":
        return "特例（上限33万円・税込）で計算"
    if t.get("mode") == "amount":
        return f"{int(t.get('yen') or 0):,}円（税込）"
    return "通常の上限で計算"


def _teiren_extra(chk):
    return "\n※価格が税込の表示で、建物の消費税が分かりません。税抜の本体価格が800万円以下かどうか、売主にご確認ください。\n" \
        if chk == "maybe" else ""


def _judge(est, result):
    """届いた資料に「手数料を変える」を出すか。計算側の判定（teiren_judge）があればそれ、なければ図面の価格から。"""
    j = getattr(est, "teiren_judge", None)
    if j in ("yes", "unknown", "no"):
        return {"yes": "yes", "unknown": "maybe"}.get(j)
    return teiren_check(result)


def ask_teiren(c, jid, a, atena="", saved=False):
    """800万円以下の物件で、会社設定が「毎回聞く」なら聞いて True（答えが届いたら finish）。"""
    chk = teiren_check(a["result"])
    if not chk or teiren_setting(c.u.get("company") or {}) != "ask":
        return False
    if not saved:
        _save_analyzed(jid, a)
    store.update_job(jid, {"宛名": atena, "teiren": None})
    store.update_user(c.uid, {"sale_tq": {"jid": jid, "step": "q", "at": store.now()}})
    c.refresh()
    c.send(texts.build("sale_teiren_q", 判定の補足=_teiren_extra(chk), drop=("postback cancel",)))
    return True


def tq_ask(c, jid):
    """届いた資料の「手数料を変える」。この資料だけ作り直す（件数に数えない）。"""
    job = store.get_job(jid) if jid else None
    if not job or job.get("user_id") != c.uid or job.get("kind") != "sale" or job.get("status") != "done":
        return c.send(line.text("申し訳ありません。この資料は作り直せませんでした。もう一度、販売図面から送ってください。"))
    store.update_user(c.uid, {"sale_tq": {"jid": jid, "step": "q", "at": store.now(), "redo": True}})
    c.refresh()
    return c.send(texts.build("sale_teiren_q", 判定の補足=_teiren_extra(job.get("teiren_check")) +
                              f"\n今の計算：{teiren_label(job.get('teiren'))}\n"))


def _tq_job(c):
    q = c.u.get("sale_tq") or {}
    jid = q.get("jid")
    job = store.get_job(jid) if jid else None
    want = "done" if q.get("redo") else "analyzed"
    if not job or job.get("status") != want or job.get("user_id") != c.uid or \
            (q.get("at") and q["at"] < store.now() - timedelta(hours=24)):
        return None, None
    return jid, job


def tq_postback(c, v):
    """tq=normal/tokurei/amount（答え）、tq=ask&doc=（届いた資料から）。"""
    if v.startswith("ask"):
        return tq_ask(c, v.partition("doc=")[2] or c.u.get("last_job"))
    jid, job = _tq_job(c)
    if not jid:
        store.update_user(c.uid, {"sale_tq": None})
        return c.send(line.text("お預かりしていた販売図面が見つかりませんでした。お手数ですが、もう一度送ってください。"))
    if v == "amount":
        store.update_user(c.uid, {"sale_tq": {**c.u["sale_tq"], "step": "amount"}})
        return c.send(texts.build("sale_teiren_amount_ask"))
    if v in ("normal", "tokurei"):
        return _tq_apply(c, jid, job, {"mode": v})
    return c.send(texts.build("sale_teiren_q", 判定の補足=""))


def tq_text(c, t):
    """手数料の金額の入力（「30万」「300000」など）。扱ったら True。"""
    q = c.u.get("sale_tq") or {}
    if q.get("step") != "amount":
        return False
    s = unicodedata.normalize("NFKC", t or "").replace(",", "").replace("円", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万)?", s)
    yen = int(round(float(m.group(1)) * (10_000 if m.group(2) else 1))) if m else 0
    if m and not m.group(2) and 0 < yen < 100:  # 「33」は万円とみなす
        yen *= 10_000
    if yen > TEIREN_MAX:
        c.send(texts.build("sale_teiren_amount_over", 送られた金額=f"{yen:,}"))
        return True
    if yen <= 0:
        c.send(texts.build("sale_teiren_amount_ask"))
        return True
    jid, job = _tq_job(c)
    if not jid:
        store.update_user(c.uid, {"sale_tq": None})
        return False
    return _tq_apply(c, jid, job, {"mode": "amount", "yen": yen}) or True


def _tq_apply(c, jid, job, t):
    redo = (c.u.get("sale_tq") or {}).get("redo")
    store.update_user(c.uid, {"sale_tq": None})
    c.refresh()
    if redo:
        url = rerender(c, jid, teiren=t)
        if not url:
            return c.send(line.text("申し訳ありません。この資料は作り直せませんでした。もう一度、販売図面から送ってください。"))
        return c.send(texts.build("sale_teiren_done", 手数料の扱い=teiren_label(t), お客様向けリンク=url))
    store.update_job(jid, {"teiren": t})
    a = _load_analyzed(jid, store.get_job(jid))
    if ask_next(c, jid, a, job.get("宛名") or "", saved=True, after="teiren"):
        return
    line.loading(c.line_uid, 60)
    return finish(c, jid, a, job.get("宛名") or "")


# ---------- 戸建て・土地：土地の評価額をどちらで出すか（路線価図／地価公示） ------------------------
# 聞くかどうか・案内する路線価図は計算側（sale_estimate.eval_question）が決める。路線価のない地域（found=False）は聞かずに地価公示で作る。
# ①路線価図：図のリンクを送り、道路の数字（例 320）の返信を evaluation.parse_rosenka_reply で円/㎡にして作る。
#   返信がないまま EVAL_WAIT_MIN 分たったら、②地価公示で作ってお知らせする（cron/often の eval_timeouts）。
# ②地価公示：すぐ作る。どちらも作るのは1回だけで、件数は1件だけ使う。

EVAL_ASK_NAMES = {"ask": "毎回聞く", "kouji": "いつも地価公示から推計"}
EVAL_WAIT_MIN = 30


def eval_setting(co):
    v = ((co or {}).get("売買設定") or {}).get("eval_ask")
    return v if v in EVAL_ASK_NAMES else "ask"


def ask_eval(c, jid, a, atena="", saved=False):
    try:
        import sale_core
        import sale_estimate
        facts = sale_core.cost_inputs(a["result"].事実)
        q = sale_estimate.eval_question(c.u.get("company") or {}, facts, (store.get_job(jid) or {}).get("evals")) if facts else None
    except (ImportError, AttributeError):
        return False
    except Exception:
        log.exception("評価額の質問の判定")
        return False
    r = (q or {}).get("rosenka") or {}
    if not r.get("found") or not r.get("maps"):
        return False
    if not saved:
        _save_analyzed(jid, a)
    store.update_job(jid, {"宛名": atena})
    store.update_user(c.uid, {"sale_eq": {"jid": jid, "step": "q", "at": store.now(), "maps": r["maps"][:6],
                                          "town": r.get("town") or ""}})
    c.refresh()
    c.send(texts.build("sale_eval_q", 町名=r.get("town") or "この物件"))
    return True


def _eq_job(c):
    q = c.u.get("sale_eq") or {}
    jid = q.get("jid")
    job = store.get_job(jid) if jid else None
    if not job or job.get("status") != "analyzed" or job.get("user_id") != c.uid or \
            (q.get("at") and q["at"] < store.now() - timedelta(hours=24)):
        return None, None
    return jid, job


def _maps_text(maps):
    if len(maps) == 1:
        return maps[0]["pdf"]
    return "\n".join(f"図{i + 1}　{m['pdf']}" for i, m in enumerate(maps))


def eq_postback(c, v):
    """eq=rosenka（①路線価図で調べる）／eq=kouji（②地価公示から推計）。"""
    jid, job = _eq_job(c)
    if not jid:
        store.update_user(c.uid, {"sale_eq": None, "sale_eq_wait": False})
        return c.send(line.text("お預かりしていた販売図面が見つかりませんでした。お手数ですが、もう一度送ってください。"))
    if v == "rosenka":
        q = c.u["sale_eq"]
        store.update_user(c.uid, {"sale_eq": {**q, "step": "wait", "at": store.now()}, "sale_eq_wait": True})
        return c.send(texts.build("sale_rosenka_ask", 路線価図=_maps_text(q.get("maps") or []),
                                  何枚=f"（{len(q.get('maps') or [])}枚あります）" if len(q.get("maps") or []) > 1 else "",
                                  待つ分数=str(EVAL_WAIT_MIN)))
    return _eq_finish(c, jid, job, None)


def eq_text(c, t):
    """路線価の返信（「320」「320D」など）。扱ったら True。"""
    q = c.u.get("sale_eq") or {}
    if q.get("step") != "wait":
        return False
    import evaluation
    v = evaluation.parse_rosenka_reply(t)
    if not v:
        c.send(texts.build("sale_rosenka_bad"))
        return True
    jid, job = _eq_job(c)
    if not jid:
        store.update_user(c.uid, {"sale_eq": None})
        return False
    return _eq_finish(c, jid, job, {"rosenka_per_m2": int(v)}) or True


def _eq_finish(c, jid, job, ev):
    store.update_user(c.uid, {"sale_eq": None, "sale_eq_wait": False})
    if ev:
        store.update_job(jid, {"evals": {**(job.get("evals") or {}), **ev}})
    c.refresh()
    line.loading(c.line_uid, 60)
    return finish(c, jid, _load_analyzed(jid, store.get_job(jid)), job.get("宛名") or "")


def eval_timeouts(users=None):
    """①を押したまま返信がない方：EVAL_WAIT_MIN 分たったら地価公示で作ってお届けする（cron/often から）。"""
    import bot
    n = 0
    cut = store.now() - timedelta(minutes=EVAL_WAIT_MIN)
    for uid, u in users if users is not None else store.db().find_all("users", "sale_eq_wait", True):
        q = u.get("sale_eq") or {}
        if q.get("step") != "wait" or not q.get("at") or q["at"] > cut or not uid.startswith("L"):
            if q.get("step") != "wait":
                store.update_user(uid, {"sale_eq_wait": False})
            continue
        c = bot.Ctx({"source": {"type": "user", "userId": uid[1:]}})
        jid, job = _eq_job(c)
        if not jid:
            store.update_user(uid, {"sale_eq": None, "sale_eq_wait": False})
            continue
        c.send(texts.build("sale_rosenka_timeout"))
        _eq_finish(c, jid, job, None)
        n += 1
    return n


QUESTIONS = (("loan", ask_loan), ("teiren", ask_teiren), ("eval", ask_eval))


def ask_next(c, jid, a, atena="", saved=False, after=None):
    """図面の直後の質問を順に（ローン → 800万円以下の手数料 → 土地の評価額の出し方）。聞いたら True。"""
    names = [n for n, _ in QUESTIONS]
    for _, f in QUESTIONS[names.index(after) + 1 if after else 0:]:
        if f(c, jid, a, atena, saved):
            return True
    return False


# ---------- お試しの期限のお知らせ（毎朝9時の定期実行から） ------------------------------------

def trial_expiry_notices(days_before=3):
    """お試しの期限（登録から14日）の3日前に1回だけ知らせる相手。戻り値 [(uid, 文面ID, 差し込み)]。
    有料プランの方・使い切った方・ブロック中の方には送らない（push の通数を使うため）。"""
    import bot
    from datetime import timedelta
    if not SALE_ONLY:
        return []
    now = store.now()
    out = []
    for uid, u in store.db().all("users"):
        e = u.get("free_expires")
        if not e or u.get("trial_notice_sent") or u.get("blocked") or u.get("review"):
            continue
        if (u.get("plan") or "free") != "free" or not (now < e <= now + timedelta(days=days_before)):
            continue
        n = store.quota(u)["remaining"]
        store.update_user(uid, {"trial_notice_sent": now})
        if n:
            out.append((uid, "sale_trial_expiring", {"期限": bot.trial_until(u), "n": n}))
    return out
