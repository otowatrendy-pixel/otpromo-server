"""売買版「内見依頼書」の見本を作り、A4 1枚・Type3なし・FAX（白黒）で読めるかを確かめる。

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

# いちばん項目が多い場合
cases = dict(bn.SAMPLES)
cases["test_max"] = dict(
    to={"company": "見本リアルエステート販売株式会社", "branch": "見本駅前支店", "person": "見本", "fax": "03-0000-0000"},
    prop={"name": "グランドパークメゾン見本タワー", "room": "1203", "address": "東京都○○区見本町10丁目11番12号",
          "price": "1億2,800万円", "number": "000000000000"},
    wishes=["11月14日（土）10時00分頃", "11月14日（土）15時30分頃", "11月15日（日）午後"], promo=bn.PROMO_MIHON)

# 売買の読み取り結果（SaleFacts の dict）から下書きを作る。FAXが空なら電話で代用しない
d = bn.from_sale_facts({"物件種別": "収益一棟", "物件名": "見本ビル", "号室": None, "所在地": "東京都○○区見本町1丁目",
                        "価格": {"記載": "1億5,000万円", "金額円": 150000000}, "元付業者": "見本住宅販売株式会社",
                        "元付業者_電話": "03-0000-1111", "元付業者_FAX": None, "物件番号": "100000000001",
                        "読み取りに自信がない項目": []})
assert d["to"]["fax"] == "" and d["to"]["check"] == ["fax"], d
assert d["prop"]["price"] == "1億5,000万円" and d["prop"]["number"] == "100000000001"
d2 = bn.from_sale_facts({"物件名": "パークメゾン見本", "号室": "503号室", "元付業者": "見本住宅販売株式会社",
                         "元付業者_FAX": "03-0000-5678", "読み取りに自信がない項目": ["元付業者"]})
assert d2["prop"]["room"] == "503" and d2["to"]["check"] == ["company"], d2
cases["test_shueki"] = dict(to={**d["to"], "fax": "03-0000-2222"}, prop=d["prop"], wishes=["11月20日（金）13時00分頃"])

groups = {out / f"{k}.pdf": [bn.html(me=bn.ME, **kw)] for k, kw in cases.items()}
# 名刺の連絡先が長いとき（はみ出さないか目で確かめる）
LONG_ME = {**bn.ME, "company": "サンプル不動産コンサルティング株式会社", "address": "東京都○○区見本町10丁目11番12号 見本ビル3階",
           "email": "taro.mihon-sales@sample-fudosan-consulting.example.jp"}
# FAXで送る版（全部黒）
groups[out / "fax_mansion.pdf"] = [bn.html(me=bn.ME, fax=True, **bn.SAMPLES["mihon_mansion"])]
assert "--ink:#000" in groups[out / "fax_mansion.pdf"][0] and "--ink:#000" not in groups[out / "mihon_mansion.pdf"][0]
groups[out / "test_long_card.pdf"] = [bn.html(me=LONG_ME, **cases["test_max"])]
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
