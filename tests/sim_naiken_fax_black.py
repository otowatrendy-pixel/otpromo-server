"""内見申込書：控え（青）とFAX送信用（黒）、QRの下の一言、QRなし、長い名前の自動縮小を見本PDFで確かめる。

架空のデータだけを使う。サーバー・faximo には何も送らない。
    python tests/sim_naiken_fax_black.py 出力フォルダ
"""
import os, sys, subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("STORE", "local")
import render
from schema import Result, Facts

out = Path(sys.argv[1] if len(sys.argv) > 1 else "naiken_mihon")
out.mkdir(parents=True, exist_ok=True)
CO = {"会社名": "サンプル不動産株式会社", "店舗名": "見本駅前店", "役職": "賃貸営業課　宅地建物取引士",
      "担当者名": "見本 太郎", "ローマ字": "TARO MIHON", "郵便番号": "000-0000", "住所": "東京都○○区見本町1丁目2番3号",
      "電話": "03-0000-1234", "FAX": "03-0000-1235", "携帯": "090-0000-0000", "メール": "mihon@example.jp",
      "免許": "東京都知事（1）第00000号"}
LONG = dict(CO, 会社名="サンプルリアルエステートマネジメント株式会社", メール="taro.mihon.chintai-eigyou@sample-fudousan.example.jp")
TO = {"company": "見本住宅管理株式会社", "branch": "", "person": "", "fax": "03-0000-5678"}
TO_LONG = {"company": "見本プロパティマネジメントサービス株式会社", "branch": "見本駅前ターミナル支店", "person": "", "fax": "03-0000-5678"}
PROP = {"name": "メゾン見本", "room": "203", "address": "東京都○○区見本町4丁目5番6号"}
W = ["11月14日（土）14時00分頃", "11月15日（日）10時00分頃"]
r = Result(事実=Facts(取引="賃貸"))

def h(company=CO, to=TO, **kw):
    return render.naiken_html(r, company, wishes=W, to=to, prop=PROP, **kw)

groups = {
    out / "mihon_hikae.pdf": [h()],                                 # スマホで見る控え（青）
    out / "mihon_fax-soushin.pdf": [h(fax=True)],                   # FAX送信用（黒）
    out / "mihon_app_fax-soushin.pdf": [h(fax=True, via="app")],    # アプリから作った申込書（一言が「アプリ」）
    out / "mihon_qr-nashi_fax-soushin.pdf": [h(company=dict(CO, 内見FAXのQR="なし"), fax=True)],
    out / "mihon_nagai-namae_fax-soushin.pdf": [h(company=LONG, to=TO_LONG, fax=True)],
}
# 確認：一言が入る・アプリは「LINE」と書かない・QRなしは一言も出ない・送信用だけ黒
assert "スマホからLINEで作りました（1分資料）" in h() and "LINE" not in h(via="app").split('class="cap')[1][:80]
assert "class=\"cap" not in h(company=dict(CO, 内見FAXのQR="なし")) and "<svg" not in h(company=dict(CO, 内見FAXのQR="なし")).split("<body>")[1]
assert "--ink:#000" in h(fax=True) and "--ink:#000" not in h()
assert "マイソク" not in h()
render.to_pdfs(groups)
for p in groups:
    subprocess.run(["pdftoppm", "-r", "110", "-png", "-singlefile", str(p), str(p.with_suffix(""))], check=True)
    print(p)
