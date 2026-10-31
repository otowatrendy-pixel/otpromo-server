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
for w in ("告知事項", "1981年", "消費税"):
    assert w in chui, w
for w in ("物件確認", "広告", "おとり"):  # 案内用の図面なので、広告の注意書きは出さない
    assert w not in chui, w
# 4) 管理費の消費税のQ&Aは直す
assert r.想定QA[0].要確認 is False and "かかりません" in r.想定QA[0].参考回答
# 5) 帯に取引態様・所属団体
assert sale_core.band_company(company)["帯_3行目"] == "取引態様：媒介"
# 6) 和暦
assert sale_core.built_year("昭和56年5月") == 1981 and sale_core.built_year("平成元年") == 1989 and sale_core.built_year("築年不詳") is None
# 6b) 「より」の価格は計算に使わない
r3 = r.model_copy(deep=True); r3.事実.価格.記載 = "7,080万円（税込）より"; r3.事実.価格.金額円 = 70800000
sale_core.tidy(r3); assert r3.事実.価格.金額円 is None and any("棟・区画" in t for t in r3.スタッフへの注意)
assert sale_core.cost_inputs(r3.事実) is None
# 6c) 固定資産税評価額：マンションで持分か分からなければ使わない
r4 = r.model_copy(deep=True); r4.事実.評価額_土地.金額円 = 90206550; r4.事実.評価額_建物.金額円 = 5000000; r4.事実.評価額_年度 = "2025年"
r4.事実.固定資産税_都市計画税 = "2025年 固都税額：372,897円"
sale_core.tidy(r4); c4 = sale_core.cost_inputs(r4.事実)
assert c4["land_eval"] is None and c4["bldg_eval"] == 5000000 and c4["annual_property_tax"] == 372897
assert any("持分" in t for t in r4.スタッフへの注意) and any("2025年" in t for t in r4.スタッフへの注意)
r4.事実.評価額_土地は持分 = True; assert sale_core.cost_inputs(r4.事実)["land_eval"] == 90206550
# 6d) 二重の点検：図面に「再建築不可」「私道負担 約12㎡」があるのに欄が空なら注意。「セットバック：無」は注意しない
r5 = r.model_copy(deep=True); r5.スタッフへの注意 = []
missed = sale_core.double_check(r5, "備考 再建築不可\n私道負担 約12㎡\nセットバック：無\n告知事項あり")
assert missed == ["再建築不可", "私道負担"], missed  # 告知事項は欄に入っているので注意しない
# 7) 賃貸図面・種別
r2 = r.model_copy(deep=True); r2.事実.取引 = "賃貸"; assert sale_core.verdict(r2) == "rental"
r2.事実.取引 = "売買"; r2.事実.物件種別 = "収益一棟"; assert sale_core.verdict(r2) == "unsupported"  # 収益・事業用は第2弾
r2.事実.物件種別 = "土地"; assert sale_core.verdict(r2) == "ok"

# 8) 購入諸費用の計算に渡す形
ci = sale_core.cost_inputs(r.事実); print(ci)
assert ci["property_type"] == "mansion" and ci["price"] == 39800000 and ci["floor_area"] == 65.12
assert ci["floor_area_is_wall_center"] and ci["structure"] == "rc" and str(ci["built"]) == "1980-04-01"
assert ci["seller_is_business"] is None and ci["kanri_hi"] == 12300 and not ci["shakuchi"]
assert [sale_core.structure_code(x) for x in ("SRC造", "鉄筋コンクリート造", "軽量鉄骨造", "鉄骨造", "木造2階建", "")] == ["src", "rc", "light_steel", "steel", "wood", None]
assert sale_core._percent("300％（前面道路により240%）") == 240 and ci["address"] == "東京都○○区見本町2丁目" and ci["commercial_area"] is False
assert sale_core.walk_min(["A線 B駅 徒歩12分", "C線 D駅 バス10分 停歩3分", "E線 F駅 徒歩7分"]) == 7.0 and ci["station_walk_min"] == 6.0
assert sale_core.first_youto("第一種低層住居専用地域・第一種中高層住居専用地域") == "第一種低層住居専用地域" and ci["youto"] == "第一種住居地域"
assert sale_core._yen("年額 12.5万円") == 125000 and sale_core._yen("98,000円") == 98000

# おすすめポイント：お客様に送る文章。強い言葉・図面にない数字のポイントは外す（10/31本人）
assert len(r.おすすめポイント) == 5, [p.見出し for p in r.おすすめポイント]  # 見本のポイントは全部残る
from schema import Point
r5 = r.model_copy(deep=True)
r5.おすすめポイント += [Point(見出し="格安の掘出し物", 説明="ぜひご検討ください。", 根拠="価格"),
                      Point(見出し="最上階の角部屋", 説明="完全分離型ではありませんが明るいお部屋です。", 根拠="所在階"),
                      Point(見出し="駅から近い", 説明="駅まで約480mです。", 根拠="交通"),
                      Point(見出し="築46年", 説明="2019年にリフォーム済みです。", 根拠="築年月"),
                      Point(見出し="新築同様", 説明="きれいなお部屋です。", 根拠="リフォーム")]
dropped = sale_core.check_points(r5, "見本線 見本駅 徒歩6分 見本東駅 徒歩11分 3LDK 65.12㎡ 8.40㎡ 1980年4月 2022年6月 8階建の3階 32戸 徒歩3分 徒歩2分 徒歩5分 39,800,000円")
assert dropped == ["格安の掘出し物", "駅から近い", "築46年", "新築同様"], dropped
assert any("「格安」" in t and "外しました" in t for t in r5.スタッフへの注意)
assert sale_core.OSUSUME_NOTE == "お客様へ送る前に内容をご確認ください"

# ③ 購入諸費用の概算書（諸費用スレッドの sale_estimate.py。置き場所にあれば一緒に作る）
SHOHI = Path("/mnt/project-files/baibai-ban/shohiyou")
estimate_html = None
if (SHOHI / "sale_estimate.py").exists():
    sys.path.insert(0, str(SHOHI))
    from datetime import date
    from sale_estimate import make_estimate
    band = {"name": company["会社名"], "license": company["免許"] + "　" + company["所属団体"], "address": company["住所"],
            "tel": company["電話"], "staff": company["担当者名"], "mobile": company["携帯"]}
    estimate_html = lambda res, comp: make_estimate(
        comp, sale_core.cost_inputs(res.事実), customer="見本 花子", property_name="メゾン見本 302号室", band=band,
        today=date(2026, 11, 2), drawing_day=date(2026, 11, 2))["html"]
out = sale_core.render_documents(company, a["img"], r, a["pages"], a["log"], estimate_html=estimate_html)
hidden = out["log"]["白で消した文字"]; print("白で消した：", hidden)
joined = "".join(hidden)
if "文字認識に失敗" in joined:  # rapidocr_onnxruntime が入っていない環境（pip install --no-deps rapidocr-onnxruntime==1.2.3）
    print("※文字認識が使えないため、白塗りの確認は飛ばします：", joined)
    hidden = None
for w in ("分かれ", "広告転載不可", "レインズ", "内見", "専任媒介", "03-0000-3333") if hidden is not None else ():
    assert w in joined, w
assert "告知" not in joined
assert "ホ" not in joined, joined  # 事業者の売主の会社名は消さない（連絡先だけ消す）
assert a["log"]["写真の補正"] == "補正なし"  # パソコンで作った画像は台形補正しない（表の枠を用紙と取り違えない）
(OUT / "obikae.jpg").write_bytes(out["obikae_jpg"])
(OUT / "okyaku.pdf").write_bytes(out["pdf_okyaku"]); (OUT / "shanai.pdf").write_bytes(out["pdf_shanai"])
assert out["texts"]["osusume_note"] == "お客様へ送る前に内容をご確認ください"
sale_schema.SaleResult.model_validate(out["result"])  # 保存の形から戻せる
print("OK", out["log"]["秒_合計"], "秒", OUT)
