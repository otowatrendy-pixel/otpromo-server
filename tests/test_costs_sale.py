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
company = {"売買設定": {"fee_mode": "half", "shihoshoshi_transfer": 70_000, "unknown_key": 1}, "売買ローン目安": {"show": True, "type": "hendo", "rates": {"hendo": 0.8}, "years": 35, "compare": True}}
facts = dict(property_type="mansion", price=49_800_000, seller_is_business=False, floor_area=62.15, built=D(2008,3,1), structure="rc", kanri_hi=15_800, shuzen_hi=12_400, address="東京都○○区見本町2丁目", youseki=200, commercial_area=False)
out = make_estimate(company, facts, {"land_eval": 10_000_000, "bldg_eval": None}, customer="見本 太郎", property_name="メゾン見本 302号室",
                    band=dict(name="サンプル不動産株式会社", license="東京都知事（1）第00000号", address="東京都○○区見本町1-2-3", tel="03-0000-0000", staff="見本 花子", mobile="090-0000-0000"), today=D(2026,10,31))
assert out["result"].lines[0].amount == 1_709_400 // 2 and len(out["simulation"]) == 3 and "住宅ローンシミュレーション" in out["html"]
assert out["simulation"][0]["monthly"] == monthly_payment(44_800_000, 0.8, 35) and out["simulation"][0]["total"] == out["simulation"][0]["monthly"] * 420
open(os.path.join(FX, "_line_sample.html"), "w").write(out["html"])
# 物件ごとの指定（LINE「借入 3500万 変動 0.9% 35年」）
o2 = make_estimate(company, facts, loan_override={"amount": 35_000_000, "type": "zenki_kotei", "rate": 2.0, "years": 30, "compare": False}, today=D(2026,10,31))
assert o2["simulation"] == [x for x in o2["simulation"]] and len(o2["simulation"]) == 1 and o2["simulation"][0]["amount"] == 35_000_000 and o2["simulation"][0]["rate"] == 2.0 and o2["simulation"][0]["years"] == 30
# 物件価格＋諸費用
o3 = make_estimate({"売買ローン目安": {"amount_mode": "price_plus_costs"}}, facts, today=D(2026,10,31))
amt = o3["simulation"][0]["amount"]; assert 49_800_000 < amt <= 49_800_000 + o3["result"].total_must + o3["result"].total_loan + 100_000
# シミュレーションなし・ローンなし
o4 = make_estimate({"売買ローン目安": {"show": False}}, facts, today=D(2026,10,31)); assert o4["simulation"] is None and "シミュレーション" not in o4["html"]
o5 = make_estimate({"売買ローン目安": {"use": False}}, facts, today=D(2026,10,31)); assert o5["result"].total_loan == 0
open(os.path.join(FX, "_sim3.html"), "w").write(out["html"]); open(os.path.join(FX, "_sim1.html"), "w").write(o2["html"])
print("ALL OK 3", out["simulation"][0], out["staff_notes"][:1])

# ---- 精度向上：条件が似た地点・駅徒歩補正・地域補正 ----
import calibration as cal
base = ev.land_unit("○○区見本町2丁目", D(2027,1,15), LD)
near_station = ev.land_unit("○○区見本町2丁目", D(2027,1,15), LD, walk_min=3, youto="第一種低層住居専用地域", youseki=200)
far_station = ev.land_unit("○○区見本町2丁目", D(2027,1,15), LD, walk_min=15, youto="第一種低層住居専用地域", youseki=200)
print(base.unit, near_station.unit, far_station.unit, near_station.label)
assert near_station.unit > far_station.unit
recs = [cal.make_record("○○区", "見本町", 300_000, 33_000_000, 100, "2027-01-0%d" % i) for i in range(1, 4)]
fac, why = cal.factor(recs, "○○区", "見本町"); assert abs(fac - 1.1) < 1e-9 and "3件" in why
assert cal.factor(recs[:2], "○○区", "見本町")[0] == 1.0
cal_u = ev.land_unit("○○区見本町2丁目", D(2027,1,15), LD, calib=fac); assert abs(cal_u.unit / base.unit - 1.1) < 0.01
ei = estimate_evals(SaleFacts("house", 58_000_000, land_area=100.5, floor_area=95.2, built=D(2012,9,1), address="○○区見本町2丁目", station_walk_min=8, youto="第一種低層住居専用地域"), D(2027,1,15), LD)
assert ei.land_unit_est and not ei.price_based
print("ALL OK 4")


# ---- 5. 2026-10-31 手直し（決済日の仮置き・借入90%・管理費翌月分・手数料の文言・火災保険）
def _t5():
    from datetime import date as D
    from costs_sale import tentative_handover, brokerage_fee, CompanySaleSettings, SaleFacts, compute, fire_estimate
    from sale_estimate import make_estimate, LOAN_DEFAULTS
    assert tentative_handover(D(2026, 10, 31)) == D(2026, 12, 1)
    assert tentative_handover(D(2026, 11, 5)) == D(2027, 1, 1)
    assert tentative_handover(D(2026, 12, 31)) == D(2027, 2, 1)
    assert LOAN_DEFAULTS["ratio"] == 0.9
    for mode in ("max", "half", "custom"):
        _, note = brokerage_fee(30_000_000, CompanySaleSettings(fee_mode=mode, fee_custom=500_000))
        assert "上限" not in note, note
    f = dict(property_type="mansion", price=30_000_000, seller_is_business=False, floor_area=70.0,
             built=D(2005, 4, 1), structure="rc", kanri_hi=20_000, shuzen_hi=10_000)
    out = make_estimate({}, f, today=D(2026, 10, 31), drawing_day=D(2026, 11, 2))
    r = out["result"]
    assert out["loan_conditions"]["ratio"] == 0.9 and "決済・お引渡し 2027年1月1日（仮）" in out["html"]
    kanri = next(l for l in r.lines if l.name == "管理費等の清算金")
    assert kanri.amount == 60_000, kanri          # 1/1引渡し：1月分（全日）＋翌月分
    staff = []
    amt, note = fire_estimate(SaleFacts(**f), CompanySaleSettings(), staff)
    # 建物1,400万：火災 16,000×1.4＝22,400、地震700万 27,500×0.7×4.7×0.9＝81,428 → 約10.4万円
    assert amt == 104_000, (amt, note)
    out2 = make_estimate({}, f, today=D(2026, 10, 31), handover_day=D(2027, 3, 15))
    assert "2027年3月15日（仮）" not in out2["html"] and "2027年3月15日" in out2["html"]
    print("ALL OK 5", amt, note)


_t5()


# ---- 6. 土地はローン関連を出さない
def _t6():
    from datetime import date as D
    from sale_estimate import make_estimate
    o = make_estimate({"売買ローン目安": {"use": True, "show": True}},
                      dict(property_type="land", price=30_000_000, seller_is_business=False, land_area=100.0), today=D(2026, 10, 31))
    r = o["result"]
    assert o["simulation"] is None and not [l for l in r.lines if l.group == "loan"]
    assert "住宅ローン関連の費用は含みません" in o["html"] and any("つなぎ融資" in n for n in r.notes)
    print("ALL OK 6")


_t6()
