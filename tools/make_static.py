"""LINEで登録のときに見せる見本画像（12色・4書体）を作る。python tools/make_static.py"""
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))
import render  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

COLORS = ["青", "紺", "水色", "緑", "深緑", "赤", "えんじ", "オレンジ", "茶", "紫", "ピンク", "黒"]


def colors_html():
    cells = ""
    for name in COLORS:
        t = render.theme({"テーマ色": name})
        cells += f"""<div class=c><div class=sw style="background:{t['main']}">{name}</div>
<div class=bar style="background:{t['light']};color:{t['main']}">見出しの色</div></div>"""
    css, fam = render.font_css({"書体": "ゴシック"})
    return f"""<!doctype html><meta charset=utf-8><style>{css}body{{margin:0;font-family:{fam};background:#fff}}
.w{{width:1040px;padding:30px;display:grid;grid-template-columns:repeat(4,1fr);gap:20px}}
h1{{grid-column:1/-1;margin:0;font-size:34px;color:#333}}.c{{border:1px solid #ddd;border-radius:14px;overflow:hidden}}
.sw{{height:120px;color:#fff;font-size:40px;font-weight:700;display:flex;align-items:center;justify-content:center}}
.bar{{padding:10px;text-align:center;font-size:22px;font-weight:700}}</style>
<div class=w><h1>テーマ色の見本（12色）</h1>{cells}</div>"""


def fonts_html():
    rows = ""
    for name in ["ゴシック", "明朝", "丸ゴシック", "UDゴシック"]:
        css, fam = render.font_css({"書体": name})
        rows += f"""<style>{css}</style><div class=r><div class=n>{name}</div>
<div class=s style="font-family:{fam}"><b>駅徒歩5分・南向きの明るいお部屋</b><br>初期費用概算書　賃料 85,000円</div></div>"""
    return f"""<!doctype html><meta charset=utf-8><style>body{{margin:0;background:#fff;font-family:sans-serif}}
.w{{width:1040px;padding:30px}}h1{{margin:0 0 16px;font-size:34px;color:#333}}
.r{{display:flex;border:1px solid #ddd;border-radius:14px;margin-bottom:16px;overflow:hidden}}
.n{{width:200px;background:#1A56A8;color:#fff;font-size:30px;font-weight:700;display:flex;align-items:center;justify-content:center}}
.s{{padding:18px 24px;font-size:30px;line-height:1.6;color:#222}}</style>
<div class=w><h1>書体の見本（4種類）</h1>{rows}</div>"""


def shot(html_text, out):
    tmp = APP / "static" / "_tmp.html"
    tmp.write_text(html_text, encoding="utf-8")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=render.CHROME)
        pg = b.new_page(viewport={"width": 1100, "height": 600}, device_scale_factor=1)
        pg.goto(tmp.as_uri(), wait_until="networkidle")
        pg.evaluate("document.fonts.ready")
        pg.locator(".w").screenshot(path=str(out))
        b.close()
    tmp.unlink()


if __name__ == "__main__":
    shot(colors_html(), APP / "static" / "colors.png")
    shot(fonts_html(), APP / "static" / "fonts.png")
    print("ok")
