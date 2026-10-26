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
import render
from prompt import SYSTEM, USER
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
    return Result.model_validate(obj), usage


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
        result, usage = call_claude(jpeg)
    t2 = time.perf_counter()
    log = {"秒_準備": round(t1 - t0, 2), "秒_AI": round(t2 - t1, 2), "モデル": config.MODEL, **usage}
    return {"img": img, "pages": pages, "result": result, "log": log}


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

    戻り値: pdf, obikae_png, obikae_jpg, obikae_preview, texts, title, pages, log, result
    """
    company = {**DEFAULT_COMPANY, **(company or {})}
    t2 = time.perf_counter()
    ob = render.obikae(img, result, company)
    ob_png = render.png_bytes(ob)
    html_text = render.build_html(result, ob_png, company)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "shiryou.pdf"
        render.to_pdf(html_text, out)
        pdf = out.read_bytes()
    t3 = time.perf_counter()

    f = result.事実
    title = f.物件名 or f.所在地 or "物件"
    shaky = list(f.読み取りに自信がない項目)
    texts = {
        "osusume": [p.model_dump() for p in result.おすすめポイント],
        "keisai": result.ポータル掲載文.model_dump(),
        "qa": [q.model_dump() for q in result.想定QA],
        "shaky": shaky,
        "ad_ok": bool(f.広告転載) and "不可" not in (f.広告転載 or ""),
    }
    log = {**(log or {}), "秒_資料": round(t3 - t2, 2)}
    log["秒_合計"] = round(log.get("秒_準備", 0) + log.get("秒_AI", 0) + log["秒_資料"], 2)
    return {
        "pdf": pdf, "obikae_png": ob_png,
        "obikae_jpg": _jpeg(ob, 3000, 88), "obikae_preview": _jpeg(ob, 1000, 80),
        "texts": texts, "title": title, "pages": pages, "log": log,
        "result": json.loads(result.model_dump_json()),
    }


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
