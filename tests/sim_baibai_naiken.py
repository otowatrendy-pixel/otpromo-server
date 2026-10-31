"""売買版「物件確認・内見依頼書」の見本を作り、A4 1枚・Type3なし・FAX（白黒）で読めるかを確かめる。

使い方： CHROME_PATH=/opt/pw-browsers/chromium python tests/sim_baibai_naiken.py [出力フォルダ]
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import baibai_naiken as bn  # noqa: E402
import render  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/baibai_naiken_test")
out.mkdir(parents=True, exist_ok=True)

# いちばん項目が多い場合（はみ出したら自動で縮む）
cases = dict(bn.SAMPLES)
cases["test_max"] = dict(
    to={"company": "見本リアルエステート販売株式会社", "branch": "見本駅前支店", "person": "見本", "fax": "03-0000-0000"},
    prop={"kind": "区分マンション", "name": "グランドパークメゾン見本タワー", "room": "1203",
          "address": "東京都○○区見本町10丁目11番12号", "price": "1億2,800万円", "number": "000000000000"},
    wishes=["11月14日（土）10時00分頃", "11月14日（土）15時30分頃", "11月15日（日）午後"],
    guests="ご夫婦とお子様2名（計4名様）", note="住宅ローンの事前審査は承認済みです", ad=True)

assert bn.kind_of("中古マンション") == "区分マンション"
assert bn.kind_of("新築一戸建て") == "戸建"
assert bn.kind_of("売地") == "土地"
assert bn.kind_of("店舗") == ""

groups = {out / f"{k}.pdf": [bn.html(me=bn.ME, **kw)] for k, kw in cases.items()}
render.to_pdfs(groups)
for pdf in groups:
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
    assert re.search(r"^Pages:\s+1$", info, re.M), (pdf, "A4 1枚に収まっていません")
    fonts = subprocess.run(["pdffonts", str(pdf)], capture_output=True, text=True).stdout
    assert "Type 3" not in fonts, (pdf, "Type3フォントが入っています")
    # FAXの細かい字（ファイン：約200dpi）で白黒2値にした画像も作って目で確かめる
    base = pdf.with_suffix("")
    subprocess.run(["pdftoppm", "-r", "200", "-mono", "-singlefile", str(pdf), str(base) + "_fax"], check=True)
    subprocess.run(["pdftoppm", "-r", "110", "-png", "-singlefile", str(pdf), str(base)], check=True)
    print("OK", pdf)
