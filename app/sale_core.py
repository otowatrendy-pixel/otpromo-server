"""売買版の資料作成の中身（読み取り→判定→スタッフ向けの注意→白塗り→帯替え→資料）。

賃貸版の core.py の仕組み（画像の読み込み・写真補正・帯替え・白塗り・料金の数え方）をそのまま使い、
AIへの指示・受け取る型・判定・白塗りの言葉・資料の中身だけを売買用にする。
賃貸版の core.py・prompt.py・schema.py は変えない。

決まったこと（10/31本人）：
- 受ける物件種別は居住用の中古マンション・戸建・土地（SALE_TYPES）。収益・事業用は第2弾
- 賃貸版とは別の専用LINE・別のサーバー窓口にする（賃貸の図面が届いたら verdict が rental を返す）
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

# 受ける物件種別（カンマ区切り）。10/31本人決定：居住用の中古マンション・戸建・土地。収益・事業用は第2弾
SALE_TYPES = {t.strip() for t in os.environ.get("SALE_TYPES", "区分マンション,戸建,土地").split(",") if t.strip()}
# 当社（客付）の取引態様。帯に「取引態様：媒介」と載せる（宅建業法34条・表示規約）
OUR_TORIHIKI = "媒介"  # 帯の取引態様はいつも「媒介」（賃貸版と同じ。会社設定や図面では変えない。11/1本人）
SALE_MOCK_RESULT = os.environ.get("SALE_MOCK_RESULT")
# 思考の深さ（low / medium / high）。文字データのあるPDFと、画像だけ（写真・スキャン・FAX）で分ける
EFFORT_TEXT = os.environ.get("SALE_EFFORT_TEXT", "low")
EFFORT_IMAGE = os.environ.get("SALE_EFFORT_IMAGE", "medium")  # 見本のJSON。入っていればAIを呼ばない（動作確認用）


# ---- 白塗り（売買の業者間の情報） ----------------------------------------------------------
# 文字認識の結果から、AIの見落としの保険として探す言葉（賃貸版の広告料・AD・鍵・内見の連絡に足す）。
# 文字認識は中国語の字形で返すことがあるので、両方の字形を入れる。
HIDE_EXTRA = re.compile(
    r"(分かれ|分れ|片手|両手|两手|"
    r"元付[^、,/]{0,6}[0-9０-９.]+\s*[%％]|客付[^、,/]{0,6}[0-9０-９.]+\s*[%％]|"
    r"レインズ|REINS|R[EＥ]INS|"
    r"広告(転載|掲載)?(可|不可|否|禁止|要承諾|要相談|要確認|NG)|(ネット|チラシ|ポータル|SUUMO|ホームズ|アットホーム)(掲載)?(可|不可|NG)|"
    r"売主[^、,/]{0,24}?(?P<c>(TEL|Tel|tel|電話|☎|FAX|Fax|fax|E-?mail|メール)\s*[:：]?\s*[0-9０-９\-－()（）a-zA-Z@.]{6,})|"
    r"infosheet|athome|at\s*home|物件番号\s*[:：]?\s*[0-9A-Za-z\-]*|情報公開日\s*[:：]?\s*[0-9./年月日]*|前回公開日\s*[:：]?\s*[0-9./年月日]*|"
    r"客付(会社|業者)?(さま|様)へ|"
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
def call_claude(jpeg: bytes, effort: str = None):
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
        output_config={"effort": effort or config.EFFORT},
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
    # 思考の深さ：文字データのあるPDFは「低」、画像だけのPDF（FAX・スキャン・文字化け）と写真は「中」（10/31本人）
    try:
        layer = text_layer(data, pages)
    except Exception:
        layer = ("", False)
    effort = EFFORT_TEXT if layer[1] else EFFORT_IMAGE
    t1 = time.perf_counter()
    # 二重の点検に使う図面の文字（文字認識は数秒かかるので、AIの読み取りと同時に進めて待ち時間を増やさない）
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=1)
    text_job = pool.submit(page_text, data, img, pages, layer)
    pool.shutdown(wait=False)
    if SALE_MOCK_RESULT:
        result, usage = SaleResult.model_validate_json(Path(SALE_MOCK_RESULT).read_text(encoding="utf-8")), {}
    else:
        try:
            result, usage = call_claude(jpeg, effort)
        except ValueError:  # JSONの形が崩れたときは1回だけやり直す
            result, usage = call_claude(jpeg, effort)
    tidy(result)
    # 二重の点検：図面の文字（PDFの文字データ＋文字認識）から大切な言葉を探し、AIの結果になければ注意を出す
    try:
        page_words = text_job.result(timeout=60)
        double_check(result, page_words)
    except Exception as ex:  # 点検ができなくても資料は作る
        page_words = ""
        result.スタッフへの注意.append(f"大切な記載の自動点検ができませんでした（{ex.__class__.__name__}）。告知事項・再建築・借地などは原本で確認してください。")
    # おすすめポイントはお客様に送る文章なので、強い言葉と図面にない数字を点検する（10/31本人）
    check_points(result, page_words)
    if pages > 1:
        # 図面の束（Excelで何件分もまとめたPDFなど）。1ページ目だけで作る
        result.スタッフへの注意.insert(0, f"PDFが{pages}ページありました。1ページ目の物件で資料を作りました。ほかのページの物件は、1件ずつ送ってください。")
    t2 = time.perf_counter()
    fix = "補正なし"
    if not pages:
        try:
            camera = scan.is_camera(Image.open(io.BytesIO(data)))
        except Exception:
            camera = False
        # パソコンで作った画像（スクリーンショット・PNG書き出し）は補正しない。
        # 白い背景に太い枠の表がある図面を「用紙の縁」と取り違えて、表だけを切り出して歪ませてしまうため。
        if camera or result.用紙.写真:
            img, fix = core.make_page(img, result, camera)
    t3 = time.perf_counter()
    log = {"種類": "売買", "秒_準備": round(t1 - t0, 2), "秒_AI": round(t2 - t1, 2), "秒_写真補正": round(t3 - t2, 2),
           "写真の補正": fix, "モデル": config.MODEL, "思考の深さ": effort, "文字データ": layer[1], **usage}
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


_MONTHLY_TAX_Q = re.compile(r"管理費|修繕積立金")


def tidy(result: SaleResult) -> None:
    """AIの答えを決まった決まりで整え、スタッフ向けの注意（社内向けにだけ出す）を足す。"""
    f = result.事実
    f.選んだ区画 = None  # 利用者が選ぶ欄。AIの答えは使わない
    if len(f.区画が複数) < 2:
        f.区画が複数 = []
    # 「7,080万円（税込）より」「3,980万円～4,280万円」のような価格は、棟・区画で変わるので計算に使わない
    if re.search(r"より|から|[~～〜]", unicodedata.normalize("NFKC", f.価格.記載 or "").replace("~", "～")) and not f.区画が複数:
        f.価格.金額円 = None
    # 取引が「不明」でも、価格が読めて賃料がない図面は売買として扱う
    if f.取引 == "不明" and (f.価格.金額円 or f.価格.記載):
        f.取引 = "売買"
    # お客様の判断に関わる記載は白で消さない
    result.帯の外の業者向け情報 = protect_list(result.帯の外の業者向け情報)
    # 売主の連絡先は、AIが業者向け情報に入れ忘れても白で消す（お客様との連絡は当社が間に入る）
    for c in list(f.売主_連絡先) + ([f.物件番号] if f.物件番号 else []):
        if c and c not in result.帯の外の業者向け情報:
            result.帯の外の業者向け情報.append(c)
    # 管理費・修繕積立金の消費税を「要確認」にしていたら直す（管理組合に払うもので課税されない）
    for q in result.想定QA:
        text = f"{q.質問} {q.参考回答}"
        if "消費税" in text and _MONTHLY_TAX_Q.search(text) and not re.search(r"仲介|手数料|価格|建物", text):
            q.参考回答 = "管理費・修繕積立金は管理組合に払うもので、消費税はかかりません。"
            q.要確認 = False
    result.スタッフへの注意 = notes(f)


def notes(f) -> list:
    """スタッフ向けの注意（お客様にお渡しする資料には載せない）。"""
    # 案内用の図面を作るサービスなので、広告（物件確認・おとり広告・広告の可否）の注意書きは出さない（10/31本人）
    out = []
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
    if f.価格.金額円 is None and f.価格.記載 and not f.区画が複数:
        out.append(f"価格が「{f.価格.記載}」で、棟・区画によって変わります。ご案内する棟・区画の価格を元付業者に確認してから概算書を作ってください。")
    if f.評価額_土地.金額円 or f.評価額_建物.金額円:
        if f.評価額_年度:
            out.append(f"固定資産税評価額は、販売図面の{f.評価額_年度}の額で計算しています（評価替えは2027年度です）。")
        if f.物件種別 in ("区分マンション", "収益区分") and f.評価額_土地.金額円 and f.評価額_土地は持分 is not True:
            out.append("土地の評価額が、敷地全体の額か、この部屋の持分の額かわかりません。登記費用の計算には使っていません。持分の額を元付業者に確認してください。")
    if f.区画が複数:
        out.append("区画・号棟ごとに価格が分かれています。概算書はご案内する区画を選んでから作ります。")
    if f.表面利回り:
        out.append(f"利回り「{f.表面利回り}」は販売図面の記載です。満室想定か現況か、賃料の明細（レントロール）を確認してください。")
    if f.読み取りに自信がない項目:
        out.append("読み取りに自信がない項目：" + "、".join(f.読み取りに自信がない項目))
    return out


# ---- 購入諸費用の計算（costs_sale.SaleFacts）に渡す形 ------------------------------------
_AREA = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(?:㎡|m2|m²|平米)")
_MANYEN = re.compile(r"([0-9,]+(?:\.[0-9]+)?)\s*万")


def _area(text):
    m = _AREA.search(unicodedata.normalize("NFKC", text or "").replace(",", ""))
    return float(m.group(1)) if m else None


def _yen(text):
    """「12,300円」「1.5万円」「年額12万円」→ 円の整数。読めなければ None。"""
    t = unicodedata.normalize("NFKC", text or "")
    m = _MANYEN.search(t)
    if m:
        return round(float(m.group(1).replace(",", "")) * 10000)
    m = re.search(r"([0-9][0-9,]*)\s*円", t)
    return int(m.group(1).replace(",", "")) if m else None


def built_date(text):
    """築年月の表記から date（その月の1日）。月が読めなければ None（計算側で要確認にする）。"""
    from datetime import date
    y = built_year(text)
    m = re.search(r"年\s*([0-9]{1,2})\s*月", unicodedata.normalize("NFKC", text or ""))
    if not y or not m or not 1 <= int(m.group(1)) <= 12:
        return None
    return date(y, int(m.group(1)), 1)


def structure_code(text):
    """構造の表記 → wood / light_steel / steel / rc / src（読めなければ None）。SRCを先に見る。"""
    st = unicodedata.normalize("NFKC", text or "").upper()
    if re.search(r"SRC|鉄骨鉄筋", st):
        return "src"
    if re.search(r"RC|鉄筋コンクリート|コンクリート", st):
        return "rc"
    if re.search(r"軽量鉄骨|軽量S|LGS", st):
        return "light_steel"
    if re.search(r"鉄骨|S造|重量", st):
        return "steel"
    if re.search(r"木|W造", st):
        return "wood"
    return None


def cost_inputs(f) -> dict:
    """読み取った事実を、購入諸費用の計算（costs_sale.SaleFacts）の引数にする。
    計算できない種別（収益・事業用など）や価格が読めないときは None。書いていない項目は None のまま。"""
    kind = {"区分マンション": "mansion", "収益区分": "mansion", "戸建": "house", "土地": "land"}.get(f.物件種別)
    price = f.価格.金額円
    if kind is None or not price or f.区画が複数 and f.選んだ区画 is None:
        return None
    if f.区画が複数 and f.選んだ区画 is not None and 0 <= f.選んだ区画 < len(f.区画が複数):
        sec = f.区画が複数[f.選んだ区画]
        price = sec.価格.金額円 or price
    tt = f.取引態様 or ""
    st = unicodedata.normalize("NFKC", f.構造 or "")
    structure = structure_code(st)
    area_text = f.専有面積 if kind == "mansion" else f.建物面積
    land_text = f.敷地持分 if kind == "mansion" else f.土地面積
    certified = f.認定住宅 or ""
    return {
        "property_type": kind,
        "price": price,
        "price_tax": f.価格_うち消費税.金額円,
        "is_new": bool(f.新築),
        "seller_is_business": True if "売主" in tt else None,  # 「代理」「媒介」では売主の種類はわからない
        "land_area": _area(land_text),
        "floor_area": _area(area_text) if kind != "land" else None,
        "floor_area_is_wall_center": not re.search(r"内法|登記|公簿", area_text or ""),
        "built": built_date(f.築年月),
        "structure": structure,
        "kanri_hi": f.管理費.金額円,
        "shuzen_hi": f.修繕積立金.金額円,
        "shuzen_kikin": f.修繕積立基金.金額円,
        "kanri_junbikin": f.管理準備金.金額円,
        "annual_property_tax": _yen(f.固定資産税_都市計画税),
        # 区分マンションで、土地の評価額が持分の額とはっきりしないときは使わない（敷地全体の額だと登記費用が何十倍にもなる）
        "land_eval": f.評価額_土地.金額円 if (kind != "mansion" or f.評価額_土地は持分 is True) else None,
        "bldg_eval": f.評価額_建物.金額円,
        "choki_yuryo": "長期優良" in certified,
        "teitanso": "低炭素" in certified,
        "kaitori_saihan": bool(f.買取再販),
        "shakuchi": bool(re.search(r"借地|地上権|賃借", f.土地権利 or "")),
        # 評価額の推計用（地価公示＋構造・延床・築年数）
        "address": f.所在地,
        "youseki": _percent(f.容積率),
        "commercial_area": bool(re.search(r"商業", f.用途地域 or "")),  # 近隣商業・商業
        "station_walk_min": walk_min(f.交通),
        "youto": first_youto(f.用途地域),
    }


def walk_min(routes):
    """交通の表記から、駅までの徒歩分のいちばん短いもの。「バス◯分」の行は使わない。なければ None。"""
    best = None
    for r in routes or []:
        t = unicodedata.normalize("NFKC", r or "")
        if re.search(r"バス|ﾊﾞｽ", t):
            continue
        for m in re.finditer(r"(?:徒歩|歩)\s*([0-9]+(?:\.[0-9]+)?)\s*分", t):
            v = float(m.group(1))
            best = v if best is None else min(best, v)
    return best


def first_youto(text):
    """用途地域の表記の最初のもの（「第一種低層住居専用地域・第一種中高層…」→ 最初）。なければ None。"""
    t = (text or "").strip()
    if not t:
        return None
    return re.split(r"[、,，/／・\s]|及び", t)[0] or None


def _percent(text):
    """「200%」「容積率 300％（前面道路により240%）」→ いちばん小さい数字（前面道路で下がった実際の容積率）。
    敷地持分の仮置きに使うため、建てられる容積に近い方を取る。読めなければ None。"""
    nums = [int(n) for n in re.findall(r"([0-9]{2,4})\s*%", unicodedata.normalize("NFKC", text or ""))]
    return min(nums) if nums else None


# ---- 二重の点検（告知事項・再建築不可などの見落としを防ぐ） ----------------------------------
# (言葉, 事実の欄, 名前, 「無し」を表す書き方なら注意しない)
CHECKS = [
    (r"告知事項|心理的瑕疵|事故物件", "告知事項", "告知事項", False),
    (r"再建築不可|再建築できません", "再建築", "再建築不可", False),
    (r"借地権|借地|地上権|定期借地|旧法借地", "土地権利", "借地", False),
    (r"私道負担|私道", "私道負担", "私道負担", True),
    (r"セットバック|SB要|道路後退", "セットバック", "セットバック", True),
    (r"市街化調整区域|調整区域", "都市計画", "市街化調整区域", False),
    (r"建築条件付|建築条件有|建築条件あり", "建築条件", "建築条件付き", False),
    (r"契約不適合|瑕疵担保|免責", "契約不適合責任", "契約不適合責任（免責など）", False),
]
_NONE_AFTER = re.compile(r"^[\s:：・]*(無|なし|無し|ナシ|不要|-|－|ー)")


def text_layer(data: bytes, pages: int) -> tuple:
    """PDFの1ページ目の文字データと、それが信用できるか（十分な量があり、文字化けしていない）。写真なら ("", False)。"""
    import subprocess
    if not pages:
        return "", False
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in.pdf"
        src.write_bytes(data)
        r = subprocess.run(["pdftotext", "-l", "1", "-layout", str(src), "-"], capture_output=True, timeout=30)
    text = unicodedata.normalize("NFKC", r.stdout.decode("utf-8", "ignore"))  # 「⾺」「⼟」のような部首の字も普通の漢字にそろえる
    # 文字化けしたPDF（独自の文字の割り当ての図面）は、文字データを信用しない
    body = re.sub(r"\s", "", text)
    normal = len(re.findall(r"[\u0020-\u007e\u3000-\u30ff\u4e00-\u9fff\uff00-\uffef㎡]", body))
    return text, len(body) >= 300 and normal / len(body) >= 0.9


def page_text(data: bytes, img, pages: int, layer=None) -> str:
    """図面の文字。信用できる文字データがあればそれを、なければ画像の文字認識も合わせたもの。"""
    import numpy as np
    text, good = layer if layer is not None else text_layer(data, pages)
    if good:  # 文字認識は数秒かかるので省く
        return unicodedata.normalize("NFKC", text)
    im = img.convert("RGB")
    if max(im.size) > 2400:
        sc = 2400 / max(im.size)
        im = im.resize((round(im.width * sc), round(im.height * sc)))
    out, _ = hide._engine()(np.asarray(im))
    return unicodedata.normalize("NFKC", text + "\n" + "\n".join(t for _, t, _ in out or []))


def double_check(result: SaleResult, text: str) -> list:
    """大切な言葉が図面にあるのに、AIの結果の欄が空なら、スタッフへの注意に「原本で確認を」と出す。見つけた名前の一覧を返す。"""
    f = result.事実
    flat = re.sub(r"\s+", "", text)
    missed = []
    for pat, key, label, none_ok in CHECKS:
        hits = list(re.finditer(pat, flat))
        if not hits or (getattr(f, key) or "").strip():
            continue
        # 「私道負担：無」「セットバック なし」だけなら注意しない
        if none_ok and all(_NONE_AFTER.match(flat[m.end():m.end() + 4]) for m in hits):
            continue
        # 契約不適合責任の「免責」は、ほかの欄（条件・備考）に書いてあれば読めている
        if any(label.split("（")[0] in x or m.group(0) in x for x in f.条件_備考 for m in hits):
            continue
        missed.append(label)
        result.スタッフへの注意.append(
            f"販売図面に「{hits[0].group(0)}」の文字がありますが、AIの読み取り結果に入っていません。原本で確認してください。")
    return missed


# ---- おすすめポイントの点検（お客様に送る文章） ------------------------------------------
OSUSUME_NOTE = "お客様へ送る前に内容をご確認ください"  # sale_render と同じ文言
# 不動産の表示に関する公正競争規約（18条など）で、根拠なく使えない言葉・断定する言葉
# 「最上階」「最寄り」「完全分離型（二世帯）」のような、事実を表す言い方は除く
BANNED = re.compile(
    r"最高|最上(?!階|部)|最適|最安|格安|破格|激安|掘り?出し?物|特選|厳選|完全(?!分離)|完璧|万全|"
    r"日本一|業界一|地域一|抜群|当社だけ|当社限り|一流|特級|お買い?得|お値打ち|絶対|必ず|"
    r"ナンバーワン|No\.?1|NO\.?1|№1|資産価値が上が|値上がり")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _nums(text: str) -> set:
    t = unicodedata.normalize("NFKC", text or "").replace(",", "")
    out = {float(x) for x in _NUM.findall(t)}
    # 和暦の年は西暦でも書けるようにする（図面「令和元年築」→ポイント「2019年築」）
    for m in _YEAR.finditer(t):
        if m.group(1):
            y = built_year(m.group(0))
            if y:
                out.add(float(y))
    return out


def point_problems(p, f, allowed: set) -> list:
    """1つのポイントの問題を返す（なければ空）。"""
    text = f"{p.見出し} {p.説明}"
    bad = []
    words = sorted({m.group(0) for m in BANNED.finditer(unicodedata.normalize("NFKC", text))})
    if not f.新築 and re.search(r"新築(?!時|当初|分譲時)", text):
        words.append("新築")
    if words:
        bad.append("使えない言葉「" + "」「".join(words) + "」")
    extra = sorted(n for n in _nums(text) if n not in allowed)
    if extra:
        bad.append("販売図面にない数字「" + "」「".join(f"{n:g}" for n in extra) + "」")
    return bad


def check_points(result: SaleResult, page_words: str) -> list:
    """おすすめポイントから、強い言葉や販売図面にない数字を含むものを外し、スタッフへの注意に残す。外した見出しの一覧を返す。

    数字は、図面の文字（文字データ＋文字認識）とAIが読み取った事実の両方にあれば使ってよい。
    図面の文字が取れなかったときは、AIが書いた根拠の文も手がかりに含める。
    """
    f = result.事実
    allowed = _nums(page_words) | _nums(f.model_dump_json())
    keep, dropped = [], []
    for p in result.おすすめポイント:
        ok_nums = allowed if page_words else allowed | _nums(p.根拠)
        bad = point_problems(p, f, ok_nums)
        if bad:
            dropped.append(p.見出し)
            result.スタッフへの注意.append(f"おすすめポイント「{p.見出し}」は、{'・'.join(bad)}があったため外しました。")
        else:
            keep.append(p)
    result.おすすめポイント = keep
    if dropped and not keep:
        result.スタッフへの注意.append("おすすめポイントが残りませんでした。販売図面を見て、お客様向けのポイントを手で書いてください。")
    return dropped


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


# ---- 帯替え・白塗り・資料 -----------------------------------------------------------------
def band_company(company: dict) -> dict:
    """帯に載せる会社情報。免許番号の行に所属団体を足し、3行目に当社の取引態様（媒介）を載せる。"""
    c = {**core.DEFAULT_COMPANY, **(company or {})}
    c["免許"] = "　".join(p for p in (c.get("免許", ""), c.get("所属団体", "")) if p)
    c["帯_3行目"] = f"取引態様：{OUR_TORIHIKI}"
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
        "osusume_note": OSUSUME_NOTE,  # お届けのときに必ず一言添える（規約3条2項と同じ内容）
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
