"""資料作成の中身。LINE とアプリの両方から呼ぶ共通の処理。

画像（またはPDF）のバイト列と会社情報 → 5つの資料（PDF）と帯替え図面、文章。
"""
import base64
import io
import json
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageOps

import config
import hide
import render
import scan
from prompt import SYSTEM, USER
import schema
from schema import Result

# 1Mトークンあたりのドル（入力, 出力, キャッシュ読み出し）。2026-10時点の公表価格
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
YEN = 150.0

# 会社情報の既定値（登録フォームで上書きする）
DEFAULT_COMPANY = {
    "会社名": "", "店舗名": "", "電話": "", "FAX": "", "住所": "", "免許": "",
    "賃貸_仲介手数料_ヶ月": 1.0, "帯画像": None, "テーマ色": "青", "書体": "ゴシック",
}


class InputError(Exception):
    """利用者に伝えるべき入力の問題（読めない画像など）。"""


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def pdf_to_image(data: bytes, dpi: int = 200) -> tuple:
    """PDFの1ページ目を高解像度の画像にする。戻り値は (画像, 総ページ数)。"""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in.pdf"
        src.write_bytes(data)
        try:
            info = subprocess.run(["pdfinfo", str(src)], capture_output=True, text=True, timeout=30).stdout
            pages = next((int(l.split()[-1]) for l in info.splitlines() if l.startswith("Pages:")), 1)
            subprocess.run(["pdftoppm", "-r", str(dpi), "-f", "1", "-l", "1", "-png", str(src), str(Path(d) / "p")],
                           check=True, capture_output=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise InputError("PDFを開けませんでした。パスワード付きや壊れたPDFでないかご確認ください。")
        png = next(Path(d).glob("p*.png"), None)
        if not png:
            raise InputError("PDFからページを取り出せませんでした。")
        return Image.open(png).convert("RGB"), pages


def load_image(data: bytes) -> tuple:
    """写真かPDFを画像にする。戻り値は (画像, PDFのページ数 or 0)。"""
    if is_pdf(data):
        return pdf_to_image(data)
    try:
        img = Image.open(io.BytesIO(data))
        return ImageOps.exif_transpose(img).convert("RGB"), 0
    except Exception:
        raise InputError("画像を読み込めませんでした。写真かPDFで送ってください。")


def _jpeg(img: Image.Image, max_edge: int, quality=85) -> bytes:
    im = img.copy()
    im.thumbnail((max_edge, max_edge))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def call_claude(jpeg: bytes):
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_KEY, base_url="https://api.anthropic.com")
    kwargs = {}
    if config.MODEL in FALLBACK_MODELS:
        kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    schema = json.dumps(Result.model_json_schema(), ensure_ascii=False)
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
    pin, pout, pcache = PRICES.get(config.MODEL, PRICES["claude-sonnet-5-5"])
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
    usd = (u.input_tokens * pin + cache_write * pin * 1.25 + cache_read * pcache + u.output_tokens * pout) / 1e6
    usage = {"入力トークン": u.input_tokens, "出力トークン": u.output_tokens, "キャッシュ読み出し": cache_read,
             "原価_円": round(usd * YEN, 2), "実際のモデル": resp.model}
    _count_cost(usd * YEN)
    return Result.model_validate(obj), usage


def _count_cost(yen):
    """AI利用料を月ごとに数える（上限の見張り：usage.ai_check）。"""
    try:
        import usage as _usage
        _usage.count("ai_yen", round(yen, 2))
    except Exception:
        pass


def analyze(data: bytes) -> dict:
    """読み取りだけ行う（AIを1回呼ぶ）。戻り値: img, pages, result, log"""
    t0 = time.perf_counter()
    img, pages = load_image(data)
    if min(img.size) < 400:
        raise InputError("画像が小さすぎて読み取れません。もう少し大きく写して送ってください。")
    jpeg = _jpeg(img, 2000)
    t1 = time.perf_counter()
    if config.MOCK_RESULT:
        result, usage = Result.model_validate_json(Path(config.MOCK_RESULT).read_text(encoding="utf-8")), {}
    else:
        try:
            result, usage = call_claude(jpeg)
        except ValueError:
            # JSONの形が崩れたときは1回だけやり直す（pydantic の検証エラーも ValueError）
            result, usage = call_claude(jpeg)
    result.事実.選んだ条件 = None  # 利用者が選ぶ欄。AIの答えは使わない
    if len(result.事実.条件が複数) < 2:
        result.事実.条件が複数 = []
    t2 = time.perf_counter()
    # AIには撮ったままの写真を送り、帯替えには補正した画像を使う。PDFはそのまま
    fix = "補正なし"
    if not pages:
        try:
            camera = scan.is_camera(Image.open(io.BytesIO(data)))
        except Exception:
            camera = False
        img, fix = make_page(img, result, camera)
    t3 = time.perf_counter()
    log = {"秒_準備": round(t1 - t0, 2), "秒_AI": round(t2 - t1, 2), "秒_写真補正": round(t3 - t2, 2),
           "写真の補正": fix, "モデル": config.MODEL, **usage}
    return {"img": img, "pages": pages, "result": result, "log": log}


def make_page(img: Image.Image, result: Result, camera: bool = False) -> tuple:
    """帯替えに使う図面の画像を作る。スマホ写真なら、用紙の切り出し・台形補正・白さの補正をする。
    図面の文字は描き直さず、画素の位置と明るさだけを整える。戻り値は (画像, 補正の内容)。"""
    paper = result.用紙
    out = None
    if paper.写真 and paper.四隅 and len(paper.四隅) == 4:
        try:
            out = scan.scan_with_corners(img, paper.四隅)
        except Exception:  # 四隅がおかしいときは、画像から探す方法に切り替える
            out = None
    if out is None:
        im = img.copy()
        if camera:
            im.info["camera"] = True
        try:
            out = scan.scan(im)
        except Exception:
            return img, "補正なし"
    page, fix, M = out
    # 帯の上の縁を、元の写真の位置から整えた図面の位置に移す（AIの「用紙に対する割合」より正確）
    b = result.帯
    if M is not None and b.見つかった and b.上端_写真 and len(b.上端_写真) == 2:
        W, H = img.size
        ys = [y for _, y in scan.to_page(M, [[x * W, y * H] for x, y in b.上端_写真])]
        top = min(ys) / page.height
        if 0.5 <= top < 0.98:
            b.上端 = top
            # 帯はふつう用紙の下端まで続くので、AIの下端が下のほうなら下端までにする
            b.下端 = 1.0 if (b.下端 or 1.0) >= 0.85 else max(b.下端, min(1.0, top + 0.06))
    return page, fix


def verdict(result: Result) -> str:
    """作ってよい資料か。ok / blurry / not_maisoku / sale / commercial"""
    f = result.事実
    if f.資料の種類 == "読み取れない":
        return "blurry"
    if f.資料の種類 == "マイソクではない":
        return "not_maisoku"
    if f.取引 != "賃貸":
        return "sale"
    if f.用途 == "事業用":
        return "commercial"
    return "ok"


def render_documents(company: dict, img: Image.Image, result: Result, pages: int = 0, log: dict = None) -> dict:
    """読み取り結果から資料一式を作る。

    戻り値: pdf（全部）, pdf_okyaku, pdf_shanai, obikae_png, obikae_jpg, obikae_preview, texts, title, pages, log, result
    """
    company = {**DEFAULT_COMPANY, **(company or {})}
    t2 = time.perf_counter()
    raw_page = img
    # 帯の外の業者向け情報（元付業者の連絡先・広告料・AD・鍵の場所など）を白で消す
    img, hidden = hide.apply(img, result)
    ob = render.obikae(img, result, company)
    ob_png = render.png_bytes(ob)
    # PDFは2つに分ける（10/28本人決定）
    #   お客様向け：帯替え図面・初期費用の概算書　社内向け：おすすめポイント・内見申込書・想定Q&A
    with tempfile.TemporaryDirectory() as d:
        okyaku, shanai = Path(d) / "okyaku.pdf", Path(d) / "shanai.pdf"
        render.to_pdfs({
            okyaku: [render.build_html(result, ob_png, company, sections=("obikae", "estimate"))],
            shanai: [render.build_html(result, ob_png, company, sections=("osusume",)),
                     render.naiken_html(result, company),
                     render.build_html(result, ob_png, company, sections=("qa",))],
        })
        pdf_okyaku, pdf_shanai = okyaku.read_bytes(), shanai.read_bytes()
        # 2つを1つにしたもの（アプリ版の pdf_url など、これまでのリンク用）
        both = Path(d) / "shiryou.pdf"
        subprocess.run(["pdfunite", str(okyaku), str(shanai), str(both)], check=True)
        pdf = both.read_bytes()
    t3 = time.perf_counter()

    f = result.事実
    title = f.物件名 or f.所在地 or "物件"
    shaky = list(f.読み取りに自信がない項目)
    texts = {
        "osusume": [p.model_dump() for p in result.おすすめポイント],
        "qa": [q.model_dump() for q in result.想定QA],
        "shaky": shaky,
        "motozuke": {"name": f.元付業者, "tel": f.元付業者_電話, "fax": f.元付業者_FAX},
    }
    log = {**(log or {}), "秒_資料": round(t3 - t2, 2), "白で消した文字": hidden}
    log["秒_合計"] = round(log.get("秒_準備", 0) + log.get("秒_AI", 0) + log.get("秒_写真補正", 0) + log["秒_資料"], 2)
    return {
        "pdf": pdf, "pdf_okyaku": pdf_okyaku, "pdf_shanai": pdf_shanai, "obikae_png": ob_png,
        # 帯を付ける前の図面（写真は補正後）。「ここを消して」の直しで作り直すときに使う
        "page_jpg": _jpeg(raw_page, 3000, 92),
        "obikae_jpg": _jpeg(ob, 3000, 88), "obikae_preview": _jpeg(ob, 1000, 80),
        "texts": texts, "title": title, "pages": pages, "log": log,
        "result": schema.for_store(result),
    }


def fix(company: dict, page: Image.Image, result: Result, obikae_png: bytes, request: str, pages: int = 0) -> dict:
    """「ここを消して」の直し。前回の読み取り結果はそのまま使い、消す文字だけをAIに聞いて足して作り直す。
    戻り値: out（render_documents と同じ。消す文字がなければ None）, texts, note（できないことの説明）, log"""
    t0 = time.perf_counter()
    texts, note, usage, served = hide.ask(Image.open(io.BytesIO(obikae_png)), request, config.MODEL, config.EFFORT)
    log = {"秒_AI": round(time.perf_counter() - t0, 2), "直しの依頼": request, "消す文字": texts,
           "直せなかったこと": note, "実際のモデル": served}
    try:
        pin, pout, pcache = PRICES.get(config.MODEL, PRICES["claude-sonnet-5-5"])
        log["原価_円"] = round((usage.input_tokens * pin + usage.output_tokens * pout) / 1e6 * YEN, 2)
        _count_cost(log["原価_円"])
    except Exception:
        pass
    if not texts:
        return {"out": None, "texts": [], "note": note, "log": log}
    result.帯の外の業者向け情報 = list(result.帯の外の業者向け情報) + texts
    out = render_documents(company, page, result, pages, log)
    return {"out": out, "texts": texts, "note": note, "log": out["log"]}


def render_fax(company: dict, img: Image.Image, result: Result) -> bytes:
    """FAX用の2ページ（帯替え図面＋初期費用概算書）を白黒で作る。"""
    company = {**DEFAULT_COMPANY, **(company or {}), "テーマ色": "#222222"}
    ob = render.fax_image(render.obikae(img, result, company))
    html_text = render.build_html(result, render.png_bytes(ob), company, sections=render.FAX_SECTIONS, bw=True)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "fax.pdf"
        render.to_pdf(html_text, out)
        return out.read_bytes()


def make_documents(company: dict, data: bytes) -> dict:
    """読み取りから資料までを一度に行う（アプリ版など確認のいらない入口用）。"""
    a = analyze(data)
    out = render_documents(company, a["img"], a["result"], a["pages"], a["log"])
    out["verdict"] = verdict(a["result"])
    return out
