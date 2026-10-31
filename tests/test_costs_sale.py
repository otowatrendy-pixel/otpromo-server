import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "app"))
from datetime import date as D
from costs_sale import *

def show(title, r):
    print("====", title)
    for l in r.lines: print(f"  [{l.group}] {l.name}: {l.amount} ({l.kind}) {l.note}")
    print("  must", r.total_must, "loan", r.total_loan, "later", r.total_later_known, "evals", r.evals_used)
    for n in r.notes: print("  注:", n)
    for n in r.staff_notes: print("  ス:", n)

s = CompanySaleSettings()
# 1 中古マンション
m = SaleFacts("mansion", 49_800_000, seller_is_business=False, floor_area=62.15, built=D(2008,3,1), structure="rc", kanri_hi=15_800, shuzen_hi=12_400)
r = compute(m, s, LoanPlan(45_000_000), contract_day=D(2026,11,20), handover_day=D(2027,1,15), today=D(2026,10,31)); show("mansion", r)
assert [l.amount for l in r.lines if l.name=="仲介手数料"][0] == int(49_800_000*0.033+66_000)
assert [l.amount for l in r.lines if l.name=="印紙税（売買契約書）"][0] == 10_000
# 期限後
r2 = compute(m, s, LoanPlan(45_000_000), contract_day=D(2027,4,10), handover_day=D(2027,5,20), today=D(2026,10,31)); show("mansion after 2027/3", r2)
assert [l.amount for l in r2.lines if l.name=="印紙税（売買契約書）"][0] == 20_000
# 52㎡ 壁芯
show("mansion 52㎡", compute(SaleFacts("mansion", 32_800_000, seller_is_business=False, floor_area=52.3, built=D(1999,5,1), kanri_hi=12000, shuzen_hi=15000), s, LoanPlan(32_800_000), D(2026,11,20), D(2027,1,15)))
# 2 中古戸建
show("house", compute(SaleFacts("house", 58_000_000, seller_is_business=False, land_area=100.5, floor_area=95.2, built=D(2012,9,1), structure="wood"), s, LoanPlan(50_000_000), D(2026,11,20), D(2027,1,15)))
# 3 新築戸建（業者売主、うち消費税）
show("new house", compute(SaleFacts("house", 62_800_000, price_tax=1_800_000, is_new=True, seller_is_business=True, land_area=90.0, floor_area=92.0, structure="wood"), s, LoanPlan(62_800_000, fee_type="hoshoryo"), D(2026,11,20), D(2027,1,15)))
# 4 土地
show("land", compute(SaleFacts("land", 35_000_000, seller_is_business=False, land_area=110.0), s, None, D(2026,11,20), D(2027,1,15)))
# 5 低廉 600万 特例
s2 = CompanySaleSettings(teiren_tokurei=True)
r5 = compute(SaleFacts("land", 6_000_000, seller_is_business=False, land_area=150.0), s2, None, D(2026,11,20), D(2027,1,15)); show("teiren", r5)
assert r5.lines[0].amount == 330_000
assert brokerage_fee(3_000_000, CompanySaleSettings())[0] == 154_000
assert brokerage_fee(2_000_000, CompanySaleSettings())[0] == 110_000
# 6 旧耐震
show("old", compute(SaleFacts("mansion", 18_000_000, seller_is_business=False, floor_area=48.0, built=D(1979,4,1)), s, None, D(2026,11,20), D(2027,1,15)))
# 7 評価額推計なし
show("no estimate", compute(m, CompanySaleSettings(estimate_eval=False), None, D(2026,11,20), D(2027,1,15)))
assert stamp_tax(9_999, True)==0 and stamp_tax(10_000, True)==200 and stamp_tax(100_001, True)==200 and stamp_tax(50_000_000, True)==10_000 and stamp_tax(50_000_001, True)==30_000
# 評価額を送ってもらって作り直し
r_est = compute(m, s, None, D(2026,11,20), D(2027,1,15))
assert any("評価額は推計" in n for n in r_est.staff_notes)
assert any("価格からの推計です。実際の評価額で変わります" in n for n in r_est.notes)
m2 = with_evals(m, land_eval=10_000_000, bldg_eval=6_000_000, annual_property_tax=140_000)
r_real = compute(m2, s, None, D(2026,11,20), D(2027,1,15)); show("mansion real evals", r_real)
assert not r_real.evals_used["estimated"] and not any("推計" in n for n in r_real.notes + r_real.staff_notes)
reg = [l for l in r_real.lines if l.name=="登記費用（所有権）"][0]
assert reg.amount == ceil_k(reg_tax(10_000_000,0.015)+reg_tax(6_000_000,0.003)+66_000+8_000)
print("ALL OK")

# ---- 地価公示・路線価からの推計（架空の見本町データ） ----
import os, evaluation as ev
FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "shohiyou")
LD = ev.LandData.load(os.path.join(FX, "landprice_points.json"), os.path.join(FX, "chome_points.json"))
h = SaleFacts("house", 58_000_000, seller_is_business=False, land_area=100.5, floor_area=95.2, built=D(2012,9,1), structure="wood", address="東京都○○区見本町2丁目")
ei = estimate_evals(h, D(2027,1,15), LD); print(ei)
assert not ei.price_based and 28_000_000 < ei.land < 30_000_000      # 約41万×0.7×100.5㎡
assert ei.bldg == int(107_000*95.2*0.39)                               # 木造 満14年 0.39
ei26 = estimate_evals(h, D(2027,5,1), LD); assert ei26.land == int(480000*0.7*100.5)   # 令和9基準年度は2026年公示
er = estimate_evals(SaleFacts("house", 58_000_000, land_area=100.0, floor_area=95.2, built=D(2012,9,1), address="○○区見本町2丁目", rosenka_per_m2=320_000), D(2027,1,15), LD)
assert er.land == 28_000_000                                            # 32万÷0.8×0.7=28万×100㎡
mm = SaleFacts("mansion", 49_800_000, seller_is_business=False, floor_area=62.15, built=D(2008,3,1), structure="rc", kanri_hi=15_800, shuzen_hi=12_400, address="東京都○○区見本町2丁目")
em = estimate_evals(mm, D(2027,1,15), LD); print(em)
assert em.bldg == int(166_000*62.15*0.5523)                             # 非木造 満18年 0.5523
et = estimate_evals(SaleFacts("land", 30_000_000, land_area=100.0, address="○○区見本町4丁目"), D(2027,1,15), LD); print(et)
eu = estimate_evals(SaleFacts("land", 30_000_000, land_area=100.0, address="△△市どこか1丁目"), D(2027,1,15), LD); assert eu.price_based
r = compute(h, CompanySaleSettings(), LoanPlan(50_000_000), D(2026,11,20), D(2027,1,15), land_data_override=LD); show("house with kouji", r)
assert any("近くの地価公示" in n for n in r.notes)
print("ALL OK 2")

# ---- LINE側の入口 ----
from sale_estimate import make_estimate, monthly_payment
assert brokerage_fee(49_800_000, CompanySaleSettings(fee_mode="rate", fee_rate=0.02))[0] == int(49_800_000*0.02*1.1)
assert brokerage_fee(3_000_000, CompanySaleSettings(fee_mode="rate", fee_rate=0.05))[0] == 154_000   # 上限で止まる
assert 120_000 < monthly_payment(45_000_000, 1.0, 35) < 135_000 and monthly_payment(45_000_000, 0, 30) == 125_000
company = {"売買設定": {"fee_mode": "half", "shihoshoshi_transfer": 70_000, "unknown_key": 1}, "売買ローン目安": {"show": True, "rate": 0.8, "years": 35}}
facts = dict(property_type="mansion", price=49_800_000, seller_is_business=False, floor_area=62.15, built=D(2008,3,1), structure="rc", kanri_hi=15_800, shuzen_hi=12_400, address="東京都○○区見本町2丁目", youseki=200, commercial_area=False)
out = make_estimate(company, facts, {"land_eval": 10_000_000, "bldg_eval": None}, customer="見本 太郎", property_name="メゾン見本 302号室",
                    band=dict(name="サンプル不動産株式会社", license="東京都知事（1）第00000号", address="東京都○○区見本町1-2-3", tel="03-0000-0000", staff="見本 花子", mobile="090-0000-0000"), today=D(2026,10,31))
assert out["result"].lines[0].amount == 1_709_400 // 2 and out["monthly"]["amount"] > 0 and "月々のご返済の目安" in out["html"]
open(os.path.join(FX, "_line_sample.html"), "w").write(out["html"])
print("ALL OK 3", out["monthly"], out["staff_notes"][:1])
