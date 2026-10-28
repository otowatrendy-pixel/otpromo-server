# リッチメニューの画像を作り直す（messages.json の richmenu から）。手元で: python3 tools/make_richmenu.py
import json, pathlib, sys
from playwright.sync_api import sync_playwright
ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"
fonts = (ROOT / "fonts/noto-sans-jp/700.css").read_text().replace("url(./files/", f"url(file://{ROOT}/fonts/noto-sans-jp/files/")
fonts += (ROOT / "fonts/noto-sans-jp/400.css").read_text().replace("url(./files/", f"url(file://{ROOT}/fonts/noto-sans-jp/files/")
ICON = {
 "撮って送る": '<path d="M20 26h10l5-7h30l5 7h10a6 6 0 0 1 6 6v40a6 6 0 0 1-6 6H20a6 6 0 0 1-6-6V32a6 6 0 0 1 6-6z"/><circle cx="50" cy="51" r="15"/>',
 "写真から送る": '<rect x="14" y="20" width="72" height="60" rx="6"/><circle cx="34" cy="40" r="7"/><path d="M14 72l24-22 16 14 12-10 20 18"/>',
 "内見申込FAX": '<rect x="22" y="14" width="56" height="34" rx="3"/><path d="M14 48h72v32H14z"/><path d="M32 60h36M32 70h24"/>',
 "のこり件数・プラン": '<circle cx="50" cy="50" r="34"/><path d="M50 30v22l14 10"/>',
 "登録内容の変更": '<path d="M24 76l6-20 36-36 14 14-36 36z"/><path d="M60 26l14 14"/>',
 "使い方・お問い合わせ": '<circle cx="50" cy="50" r="34"/><path d="M40 40a10 10 0 1 1 14 9c-3 2-4 4-4 8"/><circle cx="50" cy="68" r="1.5"/>',
 "登録をはじめる": '<circle cx="50" cy="50" r="34"/><path d="M42 34l18 16-18 16"/>',
 "使い方": '<circle cx="50" cy="50" r="34"/><path d="M40 40a10 10 0 1 1 14 9c-3 2-4 4-4 8"/><circle cx="50" cy="68" r="1.5"/>',
}
BR = {'のこり件数・プラン': 'のこり件数・<br>プラン', '使い方・お問い合わせ': '使い方・<br>お問い合わせ'}
def tile(a, big=False, main=False):
    bg = "#1A56A8" if main else "#FFFFFF"
    fg = "#FFFFFF" if main else "#1A56A8"
    sub = f'<div class="sub" style="color:{"#DCE8F7" if main else "#555"}">{a["sub"]}</div>' if a.get("sub") else ""
    return f'''<div class="t" style="background:{bg};color:{fg}"><svg viewBox="0 0 100 100" class="{'ib' if big else 'i'}" fill="none" stroke="{fg}" stroke-width="5" stroke-linecap="round" stroke-linejoin="round">{ICON[a["label"]]}</svg>
<div class="l{' lb' if big else ''}">{BR.get(a["label"], a["label"])}</div>{sub}</div>'''
css = f"""<style>{fonts}
*{{margin:0;box-sizing:border-box}} body{{font-family:'Noto Sans JP','DejaVu Sans',sans-serif;background:#C9D9EE}}
.g{{display:grid;gap:10px;padding:10px;background:#C9D9EE}}
.t{{display:flex;flex-direction:column;align-items:center;justify-content:center;border-radius:28px;text-align:center;word-break:keep-all;overflow-wrap:anywhere}}
.i{{width:190px;height:190px;margin-bottom:22px}} .ib{{width:150px;height:150px;margin-bottom:10px}}
.l{{font-weight:700;font-size:84px;line-height:1.2}} .lb{{font-size:100px}}
.sub{{font-weight:400;font-size:52px;margin-top:14px}}
</style>"""
m = json.loads((ROOT / "messages.json").read_text())["richmenu"]
reg = m["registered"]["areas"]
html_reg = css + '<div class="g" style="width:2500px;height:1686px;grid-template-columns:repeat(3,1fr);grid-template-rows:repeat(2,1fr)">' + "".join(tile(a, main=(i < 2)) for i, a in enumerate(reg)) + "</div>"
un = m["unregistered"]["areas"]
html_un = css + '<div class="g" style="width:2500px;height:843px;grid-template-columns:2fr 1fr">' + tile(un[0], big=True, main=True) + tile(un[1], big=True) + "</div>"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
    for name, html, h in (("richmenu-registered", html_reg, 1686), ("richmenu-unregistered", html_un, 843)):
        pg = b.new_page(viewport={"width": 2500, "height": h})
        pg.set_content("<html><head><meta charset=utf-8></head><body>" + html + "</body></html>")
        pg.wait_for_timeout(800)
        pg.screenshot(path=str(ROOT / "static" / f"{name}.png"), type="png")
    b.close()
print("ok")
