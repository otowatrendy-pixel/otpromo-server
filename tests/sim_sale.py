"""売買版の手元テスト（AIは見本のJSONで代用）。架空の見本図面で、判定・注意・白塗り・帯替え・PDFまで通す。

CHROME_PATH=/opt/pw-browsers/chromium python tests/sim_sale.py
"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("SALE_MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-baibai.json"))
import sale_core, sale_schema  # noqa: E402

OUT = Path(os.environ.get("OUT", "/tmp/otpromo-sale")); OUT.mkdir(parents=True, exist_ok=True)
data = (ROOT / "tests" / "fixtures" / "mihon-baibai.png").read_bytes()
company = {"会社名": "サンプル不動産株式会社", "電話": "03-0000-9999", "住所": "東京都○○区見本町1-1-1",
           "免許": "東京都知事(1)第99999号", "所属団体": "（公社）見本宅地建物取引業協会会員",
           "担当者名": "見本 太郎", "携帯": "090-0000-0000"}

a = sale_core.analyze(data)
r = a["result"]
# 1) 判定
assert sale_core.verdict(r) == "ok"
# 2) 告知事項はAIが業者向けに入れても、白塗りの対象から外す
assert not any("告知" in t for t in r.帯の外の業者向け情報), r.帯の外の業者向け情報
# 3) スタッフへの注意：物件確認・広告不可・告知事項・旧耐震・消費税不明
chui = "\n".join(r.スタッフへの注意); print(chui)
for w in ("物件確認", "広告転載不可", "告知事項", "1981年", "消費税"):
    assert w in chui, w
# 4) 管理費の消費税のQ&Aは直す
assert r.想定QA[0].要確認 is False and "かかりません" in r.想定QA[0].参考回答
# 5) 帯に取引態様・所属団体
assert sale_core.band_company(company)["帯_3行目"] == "取引態様：媒介"
# 6) 和暦
assert sale_core.built_year("昭和56年5月") == 1981 and sale_core.built_year("平成元年") == 1989 and sale_core.built_year("築年不詳") is None
# 7) 賃貸図面・種別
r2 = r.model_copy(deep=True); r2.事実.取引 = "賃貸"; assert sale_core.verdict(r2) == "rental"
sale_core.SALE_TYPES.discard("土地"); r2.事実.取引 = "売買"; r2.事実.物件種別 = "土地"; assert sale_core.verdict(r2) == "unsupported"
sale_core.SALE_TYPES.add("土地")

# 8) 購入諸費用の計算に渡す形
ci = sale_core.cost_inputs(r.事実); print(ci)
assert ci["property_type"] == "mansion" and ci["price"] == 39800000 and ci["floor_area"] == 65.12
assert ci["floor_area_is_wall_center"] and ci["structure"] == "rc" and str(ci["built"]) == "1980-04-01"
assert ci["seller_is_business"] is None and ci["kanri_hi"] == 12300 and not ci["shakuchi"]
assert [sale_core.structure_code(x) for x in ("SRC造", "鉄筋コンクリート造", "軽量鉄骨造", "鉄骨造", "木造2階建", "")] == ["src", "rc", "light_steel", "steel", "wood", None]
assert sale_core._percent("300％（前面道路により240%）") == 240 and ci["address"] == "東京都○○区見本町2丁目" and ci["commercial_area"] is False
assert sale_core._yen("年額 12.5万円") == 125000 and sale_core._yen("98,000円") == 98000

out = sale_core.render_documents(company, a["img"], r, a["pages"], a["log"])
hidden = out["log"]["白で消した文字"]; print("白で消した：", hidden)
joined = "".join(hidden)
for w in ("分かれ", "広告転載不可", "レインズ", "内見", "専任媒介"):
    assert w in joined, w
assert "告知" not in joined
(OUT / "obikae.jpg").write_bytes(out["obikae_jpg"])
(OUT / "okyaku.pdf").write_bytes(out["pdf_okyaku"]); (OUT / "shanai.pdf").write_bytes(out["pdf_shanai"])
sale_schema.SaleResult.model_validate(out["result"])  # 保存の形から戻せる
print("OK", out["log"]["秒_合計"], "秒", OUT)
