"""売買版の資料作成の中身（読み取り→判定→スタッフ向けの注意→白塗り→帯替え→資料）。

賃貸版の core.py の仕組み（画像の読み込み・写真補正・帯替え・白塗り・料金の数え方）をそのまま使い、
AIへの指示・受け取る型・判定・白塗りの言葉・資料の中身だけを売買用にする。
賃貸版の core.py・prompt.py・schema.py は変えない。

まだ決まっていないこと（本人の判断待ち。決まったら config で切り替える）：
- 受ける物件種別（SALE_TYPES）
- 賃貸版と同じLINEで振り分けるか（route_from_rental を使う）、別のLINEにするか
"""
import base64
import io
import json
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path

from PIL import Image

import config
import core
import hide
import render
import scan
from sale_prompt import SYSTEM, USER
import sale_schema
from sale_schema import SaleResult

# 受ける物件種別（カンマ区切り）。本人の決定（物件種別をどこまで入れるか）で絞る
SALE_TYPES = {t.strip() for t in os.environ.get(
    "SALE_TYPES", "区分マンション,戸建,土地,収益一棟,収益区分,事業用,その他,不明").split(",") if t.strip()}
# 当社（客付）の取引態様。帯に「取引態様：媒介」と載せる（宅建業法34条・表示規約）
OUR_TORIHIKI = os.environ.get("SALE_OUR_TORIHIKI", "媒介")
SALE_MOCK_RESULT = os.environ.get("SALE_MOCK_RESULT")  # 見本のJSON。入っていればAIを呼ばない（動作確認用）


# ---- 白塗り（売買の業者間の情報） ----------------------------------------------------------
# 文字認識の結果から、AIの見落としの保険として探す言葉（賃貸版の広告料・AD・鍵・内見の連絡に足す）。
# 文字認識は中国語の字形で返すことがあるので、両方の字形を入れる。
HIDE_EXTRA = re.compile(
    r"(分かれ|分れ|片手|両手|两手|"
    r"元付[^、,/]{0,6}[0-9０-９.]+\s*[%％]|客付[^、,/]{0,6}[0-9０-９.]+\s*[%％]|"
    r"レインズ|REINS|R[EＥ]INS|"
    r"広告(転載|掲載)?(可|不可|否|禁止|要承諾|要相談|要確認|NG)|(ネット|チラシ|ポータル|SUUMO|ホームズ|アットホーム)(掲載)?(可|不可|NG)|"
    r"業者様へ|業者間限り|業者限り|業者専用|一般公開不可|未公開|"
    r"専属専任媒介|専任媒介|一般媒介)")
# 何があっても消さない行（お客様の判断に関わる大切な記載）。AIが業者向けと間違えても消さない
PROTECT = re.compile(
    r"告知|心理的|瑕疵|事故|再建築|借地|地上権|底地|私道|セットバック|後退|調整区域|建築条件|"
    r"契約不適合|免責|ハザード|浸水|土砂|越境|境界|旧法|定期借地|価格|販売価格|うち消費税")


def protect_list(texts):
    """AIが挙げた「帯の外の業者向け情報」から、お客様の判断に関わる記載を外す（消さない）。"""
    return [t for t in texts if not PROTECT.search(unicodedata.normalize("NFKC", t))]


# ---- AI ---------------------------------------------------------------------------------
def call_claude(jpeg: bytes):
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_KEY, base_url="https://api.anthropic.com")
    kwargs = {}
    if config.MODEL in core.FALLBACK_MODELS:
        kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    schema = json.dumps(SaleResult.model_json_schema(), ensure_ascii=False)
    resp = client.beta.messages.create(
        model=config.MODEL,
        max_tokens=16000,
        system=[{"type": "text", "text": SYSTEM + "\n# 返し方\n次のJSONスキーマに合うJSONだけを返します。前後に説明やコードブロックは付けません。\n" + schema,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(jpeg).decode()}},
            {"type": "text", "text": USER},
        ]}],
        output_config={"effort": config.EFFORT},
        **kwargs,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"モデルが処理を断りました: {resp.stop_details}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("出力が上限で途切れました")
    text = "".join(b.text for b in resp.content if b.type == "text")
    obj, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
    u = resp.usage
    pin, pout, pcache = core.PRICES.get(config.MODEL, core.PRICES["claude-sonnet-5-5"])
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
    usd = (u.input_tokens * pin + cache_write * pin * 1.25 + cache_read * pcache + u.output_tokens * pout) / 1e6
    usage = {"入力トークン": u.input_tokens, "出力トークン": u.output_tokens, "キャッシュ読み出し": cache_read,
             "原価_円": round(usd * core.YEN, 2), "実際のモデル": resp.model}
    core._count_cost(usd * core.YEN)
    return SaleResult.model_validate(obj), usage


def analyze(data: bytes) -> dict:
    """読み取りだけ行う（AIを1回呼ぶ）。戻り値: img, pages, result, log"""
    t0 = time.perf_counter()
    img, pages = core.load_image(data)
    if min(img.size) < 400:
        raise core.InputError("画像が小さすぎて読み取れません。もう少し大きく写して送ってください。")
    jpeg = core._jpeg(img, 2000)
    t1 = time.perf_counter()
    if SALE_MOCK_RESULT:
        result, usage = SaleResult.model_validate_json(Path(SALE_MOCK_RESULT).read_text(encoding="utf-8")), {}
    else:
        try:
            result, usage = call_claude(jpeg)
        except ValueError:  # JSONの形が崩れたときは1回だけやり直す
            result, usage = call_claude(jpeg)
    tidy(result)
    t2 = time.perf_counter()
    fix = "補正なし"
    if not pages:
        try:
            camera = scan.is_camera(Image.open(io.BytesIO(data)))
        except Exception:
            camera = False
        img, fix = core.make_page(img, result, camera)
    t3 = time.perf_counter()
    log = {"種類": "売買", "秒_準備": round(t1 - t0, 2), "秒_AI": round(t2 - t1, 2), "秒_写真補正": round(t3 - t2, 2),
           "写真の補正": fix, "モデル": config.MODEL, **usage}
    return {"img": img, "pages": pages, "result": result, "log": log}


# ---- 読み取りのあとの決まった直し・スタッフ向けの注意 ----------------------------------------
_YEAR = re.compile(r"(明治|大正|昭和|平成|令和|[MTSHR])?\s*([0-9０-９元]{1,4})\s*年")
_ERA = {"明治": 1867, "M": 1867, "大正": 1911, "T": 1911, "昭和": 1925, "S": 1925, "平成": 1988, "H": 1988, "令和": 2018, "R": 2018}


def built_year(text):
    """築年月の表記から西暦の年を出す（和暦も）。読めなければ None。"""
    t = unicodedata.normalize("NFKC", text or "")
    m = _YEAR.search(t)
    if not m:
        return None
    n = 1 if m.group(2) == "元" else int(m.group(2))
    era = m.group(1)
    if era:
        return _ERA[era] + n
    return n if n >= 1800 else None


_KOKOKU_NG = re.compile(r"不可|否|禁止|NG|要承諾|要相談|要確認|要連絡|承諾")
_MONTHLY_TAX_Q = re.compile(r"管理費|修繕積立金")


def tidy(result: SaleResult) -> None:
    """AIの答えを決まった決まりで整え、スタッフ向けの注意（社内向けにだけ出す）を足す。"""
    f = result.事実
    f.選んだ区画 = None  # 利用者が選ぶ欄。AIの答えは使わない
    if len(f.区画が複数) < 2:
        f.区画が複数 = []
    # 取引が「不明」でも、価格が読めて賃料がない図面は売買として扱う
    if f.取引 == "不明" and (f.価格.金額円 or f.価格.記載):
        f.取引 = "売買"
    # お客様の判断に関わる記載は白で消さない
    result.帯の外の業者向け情報 = protect_list(result.帯の外の業者向け情報)
    # 管理費・修繕積立金の消費税を「要確認」にしていたら直す（管理組合に払うもので課税されない）
    for q in result.想定QA:
        text = f"{q.質問} {q.参考回答}"
        if "消費税" in text and _MONTHLY_TAX_Q.search(text) and not re.search(r"仲介|手数料|価格|建物", text):
            q.参考回答 = "管理費・修繕積立金は管理組合に払うもので、消費税はかかりません。"
            q.要確認 = False
    result.スタッフへの注意 = notes(f)


def notes(f) -> list:
    """スタッフ向けの注意（お客様にお渡しする資料には載せない）。"""
    out = ["お客様にご紹介・広告する前に、元付業者へ物件確認（販売中か・価格の変更がないか）をしてください。成約済みの物件を載せ続けるとおとり広告になります。"]
    ad = (f.広告転載 or "").strip()
    if not ad:
        out.append("募集図面に広告の可否の記載がありません。ネットやチラシに載せる場合は、先に元付業者へ確認してください。")
    elif _KOKOKU_NG.search(ad):
        out.append(f"募集図面に「{ad}」とあります。ネット・チラシなどの広告には使わず、元付業者の承諾を確認してください。")
    tt = f.取引態様 or ""
    if "売主" in tt:
        out.append("取引態様が「売主」です（図面を出している会社が売主）。建物の価格には消費税がかかり、契約不適合責任は原則として引渡しから2年以上になります。")
    elif f.価格_消費税 == "記載なし" and f.物件種別 in ("区分マンション", "戸建", "収益一棟", "収益区分", "事業用"):
        out.append("売主が個人か事業者か、価格に消費税が含まれるかが図面からわかりません。概算書の前に元付業者へ確認してください。")
    if f.告知事項:
        out.append(f"「{f.告知事項}」の記載があります。内容を元付業者に確認し、お客様へは早めに、正確にお伝えください。")
    for key, label in (("再建築", "再建築"), ("土地権利", "土地の権利"), ("都市計画", "都市計画"),
                       ("私道負担", "私道負担"), ("セットバック", "セットバック"), ("建築条件", "建築条件")):
        v = getattr(f, key) or ""
        if not v:
            continue
        if key == "再建築" and "不可" in v:
            out.append("「再建築不可」の記載があります。住宅ローンが使えない・使える金融機関が限られることがあります。")
        elif key == "土地権利" and re.search(r"借地|地上権|賃借", v):
            out.append(f"土地の権利が「{v}」です。地代・期間・更新料・譲渡や建替えの承諾、ローンの取扱いを確認してください。")
        elif key == "都市計画" and "調整" in v:
            out.append("市街化調整区域です。建替え・増築・用途の変更に制限があり、ローンが使いにくいことがあります。")
        elif key in ("私道負担", "セットバック") and not re.fullmatch(r"(無|なし|無し|ナシ|-|－)", v.strip()):
            out.append(f"{label}：「{v}」。通行・掘削の承諾や、使える土地の面積を確認してください。")
        elif key == "建築条件" and re.search(r"付|有|あり", v) and "なし" not in v:
            out.append("建築条件付きの土地です。建築請負契約の期限や、条件が外れたときの扱いを確認してください。")
    y = built_year(f.築年月)
    if y and y <= 1981 and f.物件種別 != "土地":
        out.append("築年が1981年（昭和56年）以前です。新しい耐震基準かどうかは建築確認の日付で決まるため、確認してください。住宅ローン控除や税の軽減で、耐震の証明が要る場合があります。")
    if f.区画が複数:
        out.append("区画・号棟ごとに価格が分かれています。概算書はご案内する区画を選んでから作ります。")
    if f.表面利回り:
        out.append(f"利回り「{f.表面利回り}」は募集図面の記載です。満室想定か現況か、賃料の明細（レントロール）を確認してください。")
    if f.読み取りに自信がない項目:
        out.append("読み取りに自信がない項目：" + "、".join(f.読み取りに自信がない項目))
    return out


# ---- 判定 -------------------------------------------------------------------------------
def verdict(result: SaleResult) -> str:
    """作ってよい資料か。ok / blurry / not_maisoku / rental / unsupported"""
    f = result.事実
    if f.資料の種類 == "読み取れない":
        return "blurry"
    if f.資料の種類 == "マイソクではない":
        return "not_maisoku"
    if f.取引 == "賃貸":
        return "rental"
    if f.物件種別 not in SALE_TYPES:
        return "unsupported"
    return "ok"


def route_from_rental(rental_result) -> bool:
    """賃貸版と同じLINEで受けるとき用：賃貸版の読み取りで「賃貸以外」となった図面を、売買版でもう一度読むか。
    （賃貸の図面には余分なAIの呼び出しをしない。売買の図面だけ1回多く呼ぶ＝約4〜6円）"""
    f = rental_result.事実
    return f.資料の種類 == "マイソク" and f.取引 == "賃貸以外"


# ---- 帯替え・白塗り・資料 -----------------------------------------------------------------
def band_company(company: dict) -> dict:
    """帯に載せる会社情報。免許番号の行に所属団体を足し、3行目に当社の取引態様（媒介）を載せる。"""
    c = {**core.DEFAULT_COMPANY, **(company or {})}
    c["免許"] = "　".join(p for p in (c.get("免許", ""), c.get("所属団体", "")) if p)
    c["帯_3行目"] = f"取引態様：{c.get('取引態様') or OUR_TORIHIKI}"
    return c


def render_documents(company: dict, img: Image.Image, result: SaleResult, pages: int = 0, log: dict = None,
                     estimate_html=None) -> dict:
    """読み取り結果から資料一式を作る。estimate_html(result, company) を渡すと、お客様向けPDFに概算書を入れる
    （購入諸費用の概算書は別のファイルで作る）。"""
    import sale_render
    company = band_company(company)
    t2 = time.perf_counter()
    raw_page = img
    img, hidden = hide.apply(img, result, extra=HIDE_EXTRA, protect=PROTECT)
    ob = render.obikae(img, result, company)
    ob_png = render.png_bytes(ob)
    with tempfile.TemporaryDirectory() as d:
        okyaku, shanai = Path(d) / "okyaku.pdf", Path(d) / "shanai.pdf"
        ok_pages = [sale_render.build_html(result, ob_png, company, sections=("obikae",))]
        if estimate_html:
            ok_pages.append(estimate_html(result, company))
        render.to_pdfs({
            okyaku: ok_pages,
            shanai: [sale_render.build_html(result, ob_png, company, sections=("chui", "osusume")),
                     sale_render.build_html(result, ob_png, company, sections=("qa",))],
        })
        pdf_okyaku, pdf_shanai = okyaku.read_bytes(), shanai.read_bytes()
    t3 = time.perf_counter()
    f = result.事実
    title = " ".join(x for x in (f.物件名, f.号室 and f"{f.号室}号室") if x) or f.所在地 or "物件"
    texts = {
        "osusume": [p.model_dump() for p in result.おすすめポイント],
        "qa": [q.model_dump() for q in result.想定QA],
        "chui": list(result.スタッフへの注意),
        "shaky": list(f.読み取りに自信がない項目),
        "motozuke": {"name": f.元付業者, "tel": f.元付業者_電話, "fax": f.元付業者_FAX},
    }
    log = {**(log or {}), "秒_資料": round(t3 - t2, 2), "白で消した文字": hidden}
    log["秒_合計"] = round(sum(log.get(k, 0) for k in ("秒_準備", "秒_AI", "秒_写真補正", "秒_資料")), 2)
    return {
        "pdf_okyaku": pdf_okyaku, "pdf_shanai": pdf_shanai, "obikae_png": ob_png,
        "page_jpg": core._jpeg(raw_page, 3000, 92),
        "obikae_jpg": core._jpeg(ob, 3000, 88), "obikae_preview": core._jpeg(ob, 1000, 80),
        "texts": texts, "title": title, "pages": pages, "log": log,
        "result": sale_schema.for_store(result),
    }


def make_documents(company: dict, data: bytes, estimate_html=None) -> dict:
    """読み取りから資料までを一度に行う。"""
    a = analyze(data)
    out = render_documents(company, a["img"], a["result"], a["pages"], a["log"], estimate_html)
    out["verdict"] = verdict(a["result"])
    return out
