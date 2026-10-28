"""帯替え図面の画像と、5つの資料をまとめたPDFを作る。"""
import base64
import os
import html
import io
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

import costs
from schema import Result

FONT = os.environ.get("BAND_FONT", "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf")
CHROME = os.environ.get("CHROME_PATH") or None  # 空ならPlaywright付属のChromiumを使う
# スタッフ向けの注意書き。お客様に転送される①帯替え②おすすめ③概算書と、PDFの下の余白には入れない
NOTE = "AIが作った下書きです。お客様にお渡しする前に、内容をご確認ください。"

# 初回登録で選んでもらう色。company.json の「テーマ色」に名前か #RRGGBB を入れる
PRESETS = {
    "青": "#1A56A8", "紺": "#1F2F5C", "水色": "#1E88C7", "緑": "#2E7D32", "深緑": "#1B5E4A",
    "赤": "#C62828", "えんじ": "#8E2435", "オレンジ": "#E06A00", "茶": "#7A4E2D",
    "紫": "#5E3A8C", "ピンク": "#C2185B", "黒": "#333333",
}


HERE = Path(__file__).parent
# 選べる書体（すべて商用利用できる無料フォント。SIL Open Font License）
FONTS = {
    "ゴシック": ("Noto Sans JP", "noto-sans-jp"),
    "明朝": ("Noto Serif JP", "noto-serif-jp"),
    "丸ゴシック": ("Zen Maru Gothic", "zen-maru-gothic"),
    "UDゴシック": ("BIZ UDPGothic", "biz-udpgothic"),
}


def font_css(company: dict) -> tuple:
    """company.json の「書体」から @font-face と font-family を作る。"""
    family, folder = FONTS.get(company.get("書体") or "ゴシック", FONTS["ゴシック"])
    d = HERE / "fonts" / folder
    css = ""
    for w in ("400", "700"):
        f = d / f"{w}.css"
        if f.exists():
            css += re.sub(r"url\(\./files/([^)]+)\.woff2\) format\('woff2'\), url\([^)]+\) format\('woff'\)",
                          lambda m: f"url({(d / 'files' / (m.group(1) + '.woff2')).as_uri()}) format('woff2')",
                          f.read_text(encoding="utf-8"))
    return css, f"'{family}', 'IPAGothic', sans-serif"


def _rgb(hexs):
    hexs = hexs.lstrip("#")
    return tuple(int(hexs[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def _mix(c, other, t):
    return _hex(a + (b - a) * t for a, b in zip(_rgb(c), _rgb(other)))


def _luminance(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in _rgb(c))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def theme(company: dict) -> dict:
    """メインの1色から、資料で使う濃淡を作る。白文字が読めない明るい色は自動で濃くする。"""
    main = company.get("テーマ色") or "青"
    main = PRESETS.get(main, main)
    if not (isinstance(main, str) and main.startswith("#") and len(main) == 7):
        main = PRESETS["青"]
    # 白文字とのコントラスト比 4.5 以上になるまで黒を混ぜる
    while (1.05 / (_luminance(main) + 0.05)) < 4.5:
        main = _mix(main, "#000000", 0.08)
    return {
        "main": main.upper(),
        "dark": _mix(main, "#000000", 0.25),
        "line": _mix(main, "#FFFFFF", 0.65),
        "light": _mix(main, "#FFFFFF", 0.90),
        "pale": _mix(main, "#FFFFFF", 0.95),
        "ontext": _mix(main, "#FFFFFF", 0.85),
    }


def _font(size):
    return ImageFont.truetype(FONT, size)


def draw_band(w: int, h: int, company: dict) -> Image.Image:
    """自社の帯。登録時に帯の画像をもらえればそれを使い、なければ文字で作る。"""
    staff = staff_line(company)
    if company.get("帯画像"):
        img = Image.open(company["帯画像"]).convert("RGB").resize((w, h))
        if not staff:
            return img
        # 登録した帯の画像の下に、担当者の行を足す
        sh = max(int(h * 0.28), 14)
        out = Image.new("RGB", (w, h + sh), "white")
        out.paste(img, (0, 0))
        d = ImageDraw.Draw(out)
        f = _font(int(sh * 0.62))
        d.text((w - int(sh * 0.4) - d.textlength(staff, font=f), h + (sh - f.size) // 2), staff, font=f,
               fill=theme(company)["main"])
        return out
    band = Image.new("RGB", (w, h), theme(company)["main"])
    d = ImageDraw.Draw(band)
    pad = int(h * 0.15)
    # 帯が縦に長いときに文字が大きくなりすぎて、会社名と電話番号が重ならないよう幅で抑える
    size = int(h * 0.36)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    while size > 10 and probe.textlength(company["会社名"] + f"TEL {company['電話']}", font=_font(size)) > w - pad * 4:
        size -= 2
    big, small = _font(size), _font(min(int(h * 0.2), int(size * 0.6)))
    d.text((pad, pad), company["会社名"], font=big, fill="white")
    left = company["免許"] + (f"　{staff}" if staff else "")
    d.text((pad, h - pad - small.size), left, font=small, fill="white")
    right = f"TEL {company['電話']}"
    tw = d.textlength(right, font=big)
    d.text((w - pad - tw, pad), right, font=big, fill="white")
    sub = company["住所"]
    sw = d.textlength(sub, font=small)
    d.text((w - pad - sw, h - pad - small.size), sub, font=small, fill="white")
    return band


def staff_line(company: dict) -> str:
    """帯に載せる「担当：山田 花子／090-1234-5678」。"""
    name, mobile = company.get("担当者名"), company.get("携帯")
    if not name:
        return ""
    show = company.get("携帯を載せる", True)  # 帯替え図面から携帯を外す設定（名前はいつも載せる）
    return f"担当：{name}" + (f"／{mobile}" if mobile and show else "")


def _snap_top(img: Image.Image, top: int, window: float = 0.03) -> int:
    """AIが答える帯の上端はおおよその位置なので、画像の中の実際の境目に合わせる。
    近く（高さの±3%）に帯の枠線（横幅の半分以上ある横線）があればそのすぐ上に、
    なければ文字のない行（行間）に合わせる。どちらもなければ、少し上に広げる。"""
    w, h = img.size
    g = ImageOps.grayscale(img).resize((min(w, 800), h))
    sw = g.size[0]
    lo, hi = max(0, top - int(h * window)), min(h - 1, top + int(h * window))
    rows = g.crop((0, lo, sw, hi + 1))
    data = list(rows.getdata()) if hasattr(rows, "getdata") else []
    ink = [sum(1 for v in data[i * sw:(i + 1) * sw] if v < 200) / sw for i in range(hi - lo + 1)]
    lines = [lo + i for i, f in enumerate(ink) if f > 0.5]
    if lines:
        y = min(lines, key=lambda r: abs(r - top))
        return _above_cut_text(ink, lo, max(0, y - 2), int(h * 0.04))
    gaps = [lo + i for i, f in enumerate(ink) if f < 0.003 and lo + i <= top]
    if gaps:
        return max(gaps)
    return _above_cut_text(ink, lo, max(0, top - int(h * 0.01)), int(h * 0.04))


def _above_cut_text(ink, lo: int, top: int, reach: int) -> int:
    """帯のすぐ上の行に文字がかかっていたら（上半分だけ残るのを防ぐため）、
    上に向かっていちばん文字の少ない行（行間）まで帯を広げる。"""
    i = top - lo - 1
    if i < 0 or i >= len(ink) or ink[i] < 0.01:
        return top
    stop = max(0, i - reach)
    best = min(range(stop, i + 1), key=lambda k: (ink[k], -k))
    return lo + best if ink[best] < ink[i] * 0.5 else top


def obikae(img: Image.Image, r: Result, company: dict) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = img.size
    b = r.帯
    if b.見つかった and b.上端 is not None and b.下端 is not None and 0.6 <= b.上端 < b.下端 <= 1.0:
        top, bottom = int(h * b.上端), int(h * b.下端)
        # 読み取った位置を、近くの枠線か行間に合わせる（上の文字の行を切らない）
        # 写真から切り出した図面は、AIの答える帯の位置のずれが大きいので広めに探す
        top = _snap_top(img, top, 0.03)
        band = draw_band(w, bottom - top if bottom - top > h * 0.06 else int(h * 0.08), company)
        out = img.copy()
        out.paste(band, (0, top))
        return out.crop((0, 0, w, max(bottom, top + band.height)))
    # 帯が見つからないときは、下に自社の帯を付け足す
    band = draw_band(w, int(h * 0.09), company)
    out = Image.new("RGB", (w, h + band.height), "white")
    out.paste(img, (0, 0))
    out.paste(band, (0, h))
    return out


def _src(m) -> str:
    """募集図面に記載があれば「募集図面より」、なければ「要確認」。"""
    return "募集図面より" if (m.記載 or m.金額円 is not None or m.ヶ月 is not None) else "要確認"


def _money_text(m) -> str:
    if m.金額円 is not None:
        return f"{m.金額円:,}円"
    if m.ヶ月 is not None:
        return f"{m.ヶ月:g}ヶ月"
    return m.記載 or "記載なし"


def _fee_note(text):
    """仲介手数料の記載から、業者間の配分・広告料など、お客様に見せないものを外す。"""
    if not text:
        return ""
    parts = re.split(r"[／/、,，]", text)
    keep = [p.strip() for p in parts if p.strip() and not re.search(r"配分|広告料|ＡＤ|\bAD\b|報酬|元付|客付", p)]
    return "／".join(keep)


def estimate_section(f, company: dict, name: str) -> str:
    """初期費用概算書。テナントショップ練馬のExcel版（A4縦・3区分・青の見出し帯）に合わせる。"""
    e = html.escape
    lines, known, unknown, later = costs.estimate(f, company)
    today = datetime.now(timezone(timedelta(hours=9)))
    made = f"{today.year}年{today.month}月{today.day}日"
    shop = "　".join(x for x in (company.get("会社名"), company.get("店舗名")) if x)
    biz = f.用途 == "事業用"
    months = company.get("事業用_仲介手数料_ヶ月" if biz else "賃貸_仲介手数料_ヶ月",
                         company.get("賃貸_仲介手数料_ヶ月", 1.0))

    kind = "　".join(x for x in ({"居住用": "居住用", "事業用": "事業用"}.get(f.用途), f.物件種別) if x)
    info = [("物件名", f.物件名 or "記載なし"), ("種別", kind or "記載なし"), ("所在地", f.所在地 or "記載なし"),
            ("間取り・面積", "　".join(x for x in (f.間取り, f.専有面積) if x) or "記載なし"),
            ("契約形態", f.契約期間 or "要確認"), ("入居時期", f.入居時期 or "要確認")]
    info_rows = "".join(f"<tr><th>{k}</th><td colspan=2>{e(v)}</td></tr>" for k, v in info)

    cond = [("月額賃料", _money_text(f.賃料), _src(f.賃料)),
            ("管理費・共益費", _money_text(f.管理費_共益費), _src(f.管理費_共益費)),
            *([("賃料の消費税", f.賃料_消費税, "募集図面より" if f.賃料_消費税 != "記載なし" else "要確認")]
              if biz or f.賃料_消費税 != "記載なし" else []),
            *([("敷金", _money_text(f.敷金), _src(f.敷金))]
              if _src(f.敷金) != "要確認" or _src(f.保証金) == "要確認" else []),
            *([("保証金", _money_text(f.保証金), _src(f.保証金))] if _src(f.保証金) != "要確認" else []),
            *([("償却・敷引", f.償却, "募集図面より")] if f.償却 else []),
            ("礼金", _money_text(f.礼金), _src(f.礼金)),
            ("仲介手数料", f"{months:g}ヶ月",
             "弊社規定　※別途消費税" + (f"（図面の記載：{_fee_note(f.仲介手数料_記載)}）" if _fee_note(f.仲介手数料_記載) else "")),
            ("保証会社", f.保証会社 or "記載なし", "保証会社の規定による"),
            ("火災保険", f.火災保険 or "記載なし", "要見積")]
    cond_rows = "".join(
        f"<tr><th>{k}</th><td class={'v' if v[:1].isdigit() else 't'}>{e(v)}</td><td class=m>{e(s)}</td></tr>"
        for k, v, s in cond)

    def amt(v):
        return f"{v:,}円" if v is not None else "<span class=chk>要確認</span>"
    rows = "".join(
        f"<tr><td>{e(l.項目)}</td><td class=n>{amt(l.金額円)}</td><td class=m>{e(l.メモ)}</td></tr>" for l in lines)

    later_html = ""
    if later:
        later_rows = "".join(
            f"<tr><td>{e(l.項目)}</td><td class=n>{amt(l.金額円)}</td><td class=m>{e(l.メモ)}</td></tr>" for l in later)
        later_html = f"""<h2>■ 入居後にかかる費用（参考・合計には含みません）</h2>
<table class=lines><tbody>{later_rows}</tbody></table>"""
    tel = " ／ ".join(x for x in (f"TEL {company['電話']}" if company.get("電話") else "",
                                  f"FAX {company['FAX']}" if company.get("FAX") else "") if x)
    notes = [
        "※本書は募集図面の条件をもとにした概算です。正式な金額は貸主・管理会社の請求書にてご確認ください。",
        "※入居日が決まりましたら、日割り賃料を加えた金額をご案内します。",
        "※保証会社の保証料と火災保険料は、各社の審査・見積により金額が変わります。",
        "※端数はすべて切り捨てで計算しています。",
        *(["※事業用物件として計算しています。賃料・礼金などの消費税は、図面の記載と貸主の課税状況により変わります。"] if biz else []),
        f"※お問い合わせ：{shop}　{tel}" if tel else f"※お問い合わせ：{shop}",
    ]
    # 明細が多くても必ずA4 1枚に収める（受け取り用FAXの「1回＝2枚」を崩さないため）。
    # 項目数と備考の長さで、文字・行間・余白を段階的に詰める（本文は9ptを下限）
    weight = len(lines) + len(later) + max(0, len(cond) - 7) + (1.5 if later else 0) \
        + sum(len(l.メモ or "") > 22 for l in lines + later) * 0.5
    dense = "" if weight <= 10 else " d1" if weight <= 14 else " d2" if weight <= 19 else " d3" if weight <= 25 else " d4"
    return f"""<section class="est{dense}">
<div class=band>初期費用概算書</div>
<p class=made>{e(shop)}　／　作成日：{made}</p>
<p class=cust><span>{e(company.get("宛名") or "")}</span>様</p>
<h2>■ 物件情報</h2>
<table class=kv>{info_rows}</table>
<h2>■ 算出条件</h2>
<table class=kv>{cond_rows}</table>
<h2>■ 初期費用明細</h2>
<table class=lines><thead><tr><th>項　目</th><th>金　額</th><th>備　考</th></tr></thead>
<tbody>{rows}</tbody>
<tfoot><tr><td>初期費用　合計（概算）</td><td class=n>{known:,}円</td><td class=m>{'要確認の項目は含みません' if unknown else ''}</td></tr></tfoot></table>
{later_html}
<div class=notes>{''.join(f'<p>{e(n)}</p>' for n in notes)}</div>
</section>"""


def _yen(v):
    return "要確認" if v is None else f"{v:,}円"


ALL_SECTIONS = ("obikae", "osusume", "estimate", "qa")
FAX_SECTIONS = ("obikae", "estimate")  # FAXは2ページ（帯替え図面＋初期費用概算書）


def fax_image(img: Image.Image) -> Image.Image:
    """FAX向けに白黒・高コントラストにする（細かい網点で文字がつぶれないよう、二値化はしない）。"""
    g = ImageOps.grayscale(img)
    g = ImageOps.autocontrast(g, cutoff=2)
    # 明るい背景は白に飛ばし、文字は濃くする
    return g.point(lambda v: 255 if v > 200 else int(v * 0.8)).convert("RGB")


def build_html(r: Result, obikae_png: bytes, company: dict, sections=ALL_SECTIONS, bw=False) -> str:
    e = html.escape
    f = r.事実
    img64 = base64.b64encode(obikae_png).decode()
    name = f.物件名 or f.所在地 or "物件"

    points = "".join(
        f"<li><b>{e(p.見出し)}</b><p>{e(p.説明)}</p><small>根拠：{e(p.根拠)}</small></li>" for p in r.おすすめポイント)
    qa = "".join(
        f"<dt>Q{i}. {e(q.質問)}{' <span class=chk>要確認</span>' if q.要確認 else ''}</dt><dd>{e(q.参考回答)}</dd>"
        for i, q in enumerate(r.想定QA, 1))
    shaky = ""
    if f.取引 != "賃貸":
        shaky += "<p class=warn>賃貸以外の募集図面のようです。このサービスは賃貸物件専用のため、内容をよくご確認ください。</p>"
    if f.読み取りに自信がない項目:
        shaky += f"<p class=warn>読み取りに自信がない項目：{e('、'.join(f.読み取りに自信がない項目))}</p>"

    if bw:
        company = {**company, "テーマ色": "#222222"}
    t = theme(company)
    fcss, family = font_css(company)
    parts = {
        "obikae": f"""<section><h1>① 帯替え図面<small>{e(name)}</small></h1>
<img src="data:image/png;base64,{img64}">{shaky}</section>""",
        "osusume": f"""<section><h1>② おすすめポイント<small>{e(name)}</small></h1><ol class=pt>{points}</ol></section>""",
        "estimate": estimate_section(f, company, name),
        "qa": f"""<section><h1>⑤ 想定Q&amp;A<small>{e(name)}</small></h1><dl>{qa}</dl>
<p class=note>{NOTE}</p></section>""",
    }
    body = "\n".join(parts[k] for k in sections)
    return f"""<!doctype html><html lang=ja><meta charset=utf-8><style>
{fcss}
:root {{ --main: {t['main']}; --dark: {t['dark']}; --line: {t['line']}; --light: {t['light']}; --pale: {t['pale']}; --ontext: {t['ontext']}; --font: {family}; }}
@page {{ size: A4; margin: 14mm 14mm 16mm; }}
body {{ font-family: var(--font); color: #222; font-size: 10.5pt; line-height: 1.7;
  word-break: auto-phrase; line-break: strict; }}
h1 {{ color: var(--main); font-size: 16pt; border-bottom: 3px solid var(--main); padding-bottom: 4px; margin: 0 0 10px; }}
h1 small {{ font-size: 9pt; color: #555; margin-left: 8px; }}
section {{ page-break-after: always; }}
section:last-child {{ page-break-after: auto; }}
img {{ width: 100%; border: 1px solid #ccc; }}
ol.pt li {{ margin-bottom: 10px; }} ol.pt p {{ margin: 2px 0; }} small {{ color: #666; }}
table {{ width: 100%; border-collapse: collapse; }} td {{ border-bottom: 1px solid #dde; padding: 5px 4px; vertical-align: top; }}
td.n {{ text-align: right; white-space: nowrap; width: 8em; }} tr.total td {{ font-weight: bold; border-top: 2px solid var(--main); }}
dt {{ font-weight: bold; margin-top: 8px; }} dd {{ margin: 2px 0 0 1.2em; }}
.chk {{ background: var(--light); color: var(--main); font-size: 8pt; padding: 1px 6px; border-radius: 8px; }}
.warn {{ background: var(--pale); border-left: 4px solid var(--main); padding: 6px 10px; font-size: 9.5pt; }}
.portal {{ border: 1px solid #ccd; padding: 10px 12px; border-radius: 6px; }}
.note {{ font-size: 8.5pt; color: #666; margin-top: 12px; }}
.est {{ font-family: var(--font); font-size: 10pt; line-height: 1.5; }}
.est .band {{ background: var(--main); color: #fff; font-size: 18pt; font-weight: bold; text-align: center; padding: 8px 0; letter-spacing: .2em; }}
.est .made {{ text-align: right; font-size: 9pt; color: #555566; margin: 4px 0 10px; }}
.est .cust {{ font-size: 12pt; font-weight: bold; margin: 0 0 6px; }}
.est .cust span {{ display: inline-block; width: 18em; border-bottom: 1px solid #222; margin-right: 6px; }}
.est h2 {{ color: var(--main); font-size: 11pt; margin: 12px 0 4px; }}
.est table {{ border-collapse: collapse; width: 100%; }}
.est th, .est td {{ border: 1px solid var(--line); padding: 3px 8px; vertical-align: middle; }}
.est .kv th {{ background: var(--light); text-align: left; width: 30%; font-weight: bold; }}
.est .kv td.v {{ width: 25%; text-align: right; }}
.est .kv td.t {{ width: 25%; }}
.est td.m {{ font-size: 9pt; color: #555566; }}
.est .lines thead th {{ background: var(--main); color: #fff; text-align: center; }}
.est .lines td:first-child {{ width: 33%; }}
.est .lines td.n {{ width: 20%; text-align: right; white-space: nowrap; }}
.est .lines tfoot td {{ background: var(--main); color: #fff; font-weight: bold; font-size: 12pt; border-color: var(--main); }}
.est .lines tfoot td.n {{ font-size: 13pt; }}
.est .lines tfoot td.m {{ color: var(--ontext); font-size: 8.5pt; font-weight: normal; }}
.est .notes {{ margin-top: 10px; font-size: 9pt; color: #444444; }}
.est .notes p {{ margin: 2px 0; }}
.est.d1 {{ font-size: 9.5pt; line-height: 1.35; }}
.est.d1 th, .est.d1 td {{ padding: 2px 7px; }}
.est.d1 h2 {{ margin: 8px 0 3px; }}
.est.d2, .est.d3, .est.d4 {{ font-size: 9pt; line-height: 1.25; }}
.est.d2 .band, .est.d3 .band, .est.d4 .band {{ font-size: 15pt; padding: 4px 0; }}
.est.d2 th, .est.d2 td {{ padding: 1px 6px; }}
.est.d2 h2, .est.d3 h2, .est.d4 h2 {{ font-size: 10pt; margin: 6px 0 2px; }}
.est.d2 td.m, .est.d3 td.m, .est.d4 td.m {{ font-size: 8pt; }}
.est.d2 .notes, .est.d3 .notes, .est.d4 .notes {{ font-size: 8pt; margin-top: 6px; }}
.est.d2 .notes p, .est.d3 .notes p, .est.d4 .notes p {{ margin: 0; }}
.est.d3 {{ line-height: 1.15; }}
.est.d3 th, .est.d3 td, .est.d4 th, .est.d4 td {{ padding: 0 5px; }}
.est.d3 .made, .est.d4 .made {{ margin: 2px 0 4px; }}
.est.d4 {{ line-height: 1.1; zoom: .85; }}
</style>
{body}</html>"""


def to_pdf(html_text, out: Path):
    """HTMLをPDFにする。html_text に並びを渡すと、順にPDFにして1つにまとめる（pdfunite）。"""
    htmls = [html_text] if isinstance(html_text, str) else list(html_text)
    to_pdfs({out: htmls})


def to_pdfs(groups: dict):
    """{出力先: [HTML, …]} をまとめてPDFにする。ブラウザの起動は1回で済ませる。"""
    import shutil
    import subprocess
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        page = b.new_page()
        for out, htmls in groups.items():
            parts = []
            for i, h in enumerate(htmls):
                # 書体ファイルを読み込めるよう、いったんファイルに書いてから開く
                tmp = out.with_suffix(f".{i}.print.html")
                tmp.write_text(h, encoding="utf-8")
                page.goto(tmp.as_uri(), wait_until="networkidle")
                page.evaluate("document.fonts.ready")
                part = out.with_suffix(f".{i}.pdf")
                page.pdf(path=str(part), format="A4", print_background=True)
                tmp.unlink()
                parts.append(part)
            if len(parts) > 1:
                subprocess.run(["pdfunite", *map(str, parts), str(out)], check=True)
                for part in parts:
                    part.unlink()
            else:
                shutil.move(str(parts[0]), str(out))
        b.close()


NAIKEN_TEMPLATE = HERE / "naiken_template.html"
WEEK = "月火水木金土日"


def naiken_html(r: Result, company: dict, wishes=None, to=None, me=None, guests=None, prop=None) -> str:
    """内見申込書（FAX用）。テンプレートは内見FAX担当が作った naiken_template.html（Jinja2）。

    資料一式に入れるときは希望日時を空欄にし、元付業者と自社の情報だけ入れておく。
    """
    from jinja2 import Template
    f = r.事実
    today = datetime.now(timezone(timedelta(hours=9)))
    blank = "　　月　　日（　　）　　時　　分頃"
    me = me or {
        "company": company.get("会社名", ""), "branch": company.get("店舗名", ""), "role": company.get("役職", ""),
        "name": company.get("担当者名", ""), "family_name": company.get("担当者姓") or "担当者",
        "roman": company.get("ローマ字", ""), "zip": company.get("郵便番号", ""), "address": company.get("住所", ""),
        "tel": company.get("電話", ""), "fax": company.get("FAX", ""), "mobile": company.get("携帯", ""),
        "email": company.get("メール", ""), "license": company.get("免許", ""),
    }
    room = None
    name = f.物件名 or ""
    m = re.search(r"\s*(\d+)\s*号室?$", name)
    if m:
        name, room = name[:m.start()].strip(), m.group(1)
    data = {
        "sent_date": f"{today.year}年{today.month}月{today.day}日（{WEEK[today.weekday()]}）",
        "to": to or {"company": f.元付業者 or "", "branch": "", "person": "", "fax": f.元付業者_FAX or ""},
        "prop": prop or {"name": name, "room": room, "address": f.所在地 or ""},
        # 空文字の希望は手書きできる空欄の行にする（アプリの「空欄にする」）。全部空なら2行とも空欄
        "wishes": [w or blank for w in wishes] if wishes else [blank, blank], "guests": guests, "me": me,
    }
    tpl = NAIKEN_TEMPLATE.read_text(encoding="utf-8")
    # 書体はネットから読まず、同梱のものを使う
    tpl = re.sub(r'<link href="https://fonts\.googleapis\.com[^>]*>', "", tpl)
    css = font_css({"書体": "ゴシック"})[0] + font_css({"書体": "明朝"})[0]
    # 一番下の友だち追加QR（内見申込書FAX専用の経路 https://lin.ee/YzyWFNQ、15mm角。10/29本人承認）
    from markupsafe import Markup
    qr = HERE / "static" / "naiken-qr.svg"
    data.setdefault("qr_svg", Markup(qr.read_text(encoding="utf-8")) if qr.exists() else "")
    html_text = Template(tpl, autoescape=True).render(**data)
    return html_text.replace("<style>", "<style>\n" + css, 1)


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
