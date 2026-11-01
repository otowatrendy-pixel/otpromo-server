"""LINE（sale_bot.py）から概算書を作るときの入口。

    from sale_estimate import make_estimate
    out = make_estimate(company, facts_dict, evals=None, customer="見本 太郎", property_name="メゾン見本 302号室",
                        band={...}, contract_day=None, handover_day=None, loan_amount=None, today=None)
    out["html"]         … お客様向けA4 1枚のHTML（render_pdf と同じChromiumでPDF化）
    out["staff_notes"]  … スタッフ向けの確認事項（お客様向けPDFには入れない。LINEの文面に添える）
    out["result"]       … 計算結果（Result）

company["売買設定"]      … CompanySaleSettings と同じキー名（知らないキーは無視）
company["売買ローン目安"] … 住宅ローンの設定（下の LOAN_DEFAULTS と同じキー名）
loan_override            … 物件ごとの指定 {amount, type, rate, years, kotei_years}（あるキーだけ会社設定より優先）
evals                    … スタッフがLINEで送った値 {"land_eval","bldg_eval","annual_property_tax",
                             "rosenka_per_m2","kotei_rosenka_per_m2"}（あるものだけ）
drawing_day              … 図面を送った日（日本時間）。
teiren                   … 物件ごとの800万円以下の特例 {"mode": "normal"/"tokurei"/"amount", "yen": 金額}（会社設定より優先）。
                             戻り値 teiren_judge＝yes/no/unknown（税抜800万円以下か。unknown は業者売主で消費税が不明）
契約日が None なら作成日、決済日（引渡日）が None なら drawing_day（なければ作成日）の翌々月1日で仮置き
（概算書に「（仮）」と出る）。スタッフが8桁で送った日は handover_day に入れる（確定扱い）。
"""
from __future__ import annotations

import dataclasses
import os
from datetime import date, timedelta
from typing import Optional

from costs_sale import CompanySaleSettings, LoanPlan, SaleFacts, compute, tentative_handover
from render_sale import build_html

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL_KEYS = ("land_eval", "bldg_eval", "annual_property_tax", "rosenka_per_m2", "kotei_rosenka_per_m2")


def settings_from_company(company: dict) -> CompanySaleSettings:
    raw = dict(company.get("売買設定") or {})
    names = {f.name for f in dataclasses.fields(CompanySaleSettings)}
    return CompanySaleSettings(**{k: v for k, v in raw.items() if k in names})


def monthly_payment(amount: int, rate_pct: float, years: int) -> int:
    """元利均等返済の毎月の返済額（ボーナス払いなし）。"""
    n = years * 12
    r = rate_pct / 100 / 12
    if r == 0:
        return int(amount / n)
    return int(amount * r / (1 - (1 + r) ** -n))


LOAN_DEFAULTS = {
    "use": True,                 # 住宅ローンを使う前提で諸費用を出すか（ローン関連費用の欄）
    "show": True,                # 住宅ローンシミュレーションを付けるか
    "type": "hendo",             # 初期の金利タイプ：hendo 変動 / kotei_kikan 固定期間選択 / zenki_kotei 全期間固定
    # 2026年10月の水準：変動は大手の最優遇1.195%、10年固定は大手3.6〜4.75%、フラット35最低3.64%（会社設定で変更可）
    "rates": {"hendo": 1.2, "kotei_kikan": 3.7, "zenki_kotei": 3.6},
    "kotei_years": 10,           # 固定期間選択の固定年数
    "years": 35,                 # 借入年数
    "amount_mode": "ratio",      # ratio＝物件価格×ratio / price_plus_costs＝物件価格＋諸費用 / fixed＝amount
    "ratio": 0.9,                 # 初期は物件価格の90%（2026-10-31 本人指示）
    "amount": None,
    "compare": False,            # True なら3タイプを並べて比べる
}
TYPE_JA = {"hendo": "変動金利", "kotei_kikan": "固定期間選択", "zenki_kotei": "全期間固定"}


def loan_conditions(company: dict, loan_override: Optional[dict]) -> dict:
    c = {**LOAN_DEFAULTS, **(company.get("売買ローン目安") or {})}
    c["rates"] = {**LOAN_DEFAULTS["rates"], **(c.get("rates") or {})}
    if c.get("rate") is not None and "rates" not in (company.get("売買ローン目安") or {}):
        c["rates"][c["type"]] = float(c["rate"])        # 古い形（rate だけ）との互換
    for k, v in (loan_override or {}).items():
        if v is not None:
            c[k] = v
    if (loan_override or {}).get("rate") is not None:
        c["rates"][c["type"]] = float(loan_override["rate"])
    if (loan_override or {}).get("amount") is not None:
        c["amount_mode"] = "fixed"
    return c


def simulate(amount: int, c: dict) -> list[dict]:
    types = ["hendo", "kotei_kikan", "zenki_kotei"] if c.get("compare") else [c["type"]]
    out = []
    for t in types:
        rate = float(c["rates"][t])
        years = int(c["years"])
        m = monthly_payment(amount, rate, years)
        label = TYPE_JA[t] + (f"（当初{int(c['kotei_years'])}年固定）" if t == "kotei_kikan" else "")
        out.append({"type": t, "label": label, "rate": rate, "years": years, "amount": amount,
                    "monthly": m, "yearly": m * 12, "total": m * 12 * years, "selected": t == c["type"]})
    return out


def eval_question(company: dict, facts: dict, evals: Optional[dict] = None) -> Optional[dict]:
    """図面を読んだ直後に「土地の評価額をどちらで出しますか？」を聞くか。聞かないなら None。
    聞くのは戸建て・土地で、販売図面にもLINEにも評価額・路線価がなく、会社設定が eval_ask="ask" のとき。
    マンションは敷地持分が小さく税金への影響がわずかなので聞かない（地価公示から自動で推計）。
    戻り値 {"rosenka": rosenka_maps(...)}：①を押されたときに送るリンク。found=False なら①は出さない
    （路線価のない地域）か、index_page（町丁名の一覧）を案内する。"""
    import evaluation as ev
    s = settings_from_company(company)
    if s.eval_ask != "ask" or facts.get("property_type") not in ("house", "land") or facts.get("shakuchi"):
        return None
    have = {**facts, **(evals or {})}
    if any(have.get(k) for k in ("land_eval", "rosenka_per_m2", "kotei_rosenka_per_m2")):
        return None
    return {"rosenka": ev.rosenka_maps(facts.get("address"))}


def make_estimate(company: dict, facts: dict, evals: Optional[dict] = None, *, customer: str = "",
                  property_name: str = "", band: Optional[dict] = None, contract_day: Optional[date] = None,
                  handover_day: Optional[date] = None, loan_amount: Optional[int] = None,
                  loan_override: Optional[dict] = None, today: Optional[date] = None,
                  font_dir: Optional[str] = None, drawing_day: Optional[date] = None,
                  teiren: Optional[dict] = None) -> dict:
    today = today or date.today()
    f = SaleFacts(**facts)
    if evals:
        f = dataclasses.replace(f, **{k: v for k, v in evals.items() if k in EVAL_KEYS and v is not None})
    s = settings_from_company(company)
    if loan_amount is not None:
        loan_override = {**(loan_override or {}), "amount": loan_amount}
    c = loan_conditions(company, loan_override)
    cd = contract_day or today
    hd = handover_day or tentative_handover(drawing_day or today)   # 仮＝図面を送った日の翌々月1日

    def floor100k(x):
        return int(x // 100_000 * 100_000)

    loan = None
    if c["use"] and f.property_type != "land":   # 土地はローン関連・シミュレーションを出さない
        if c["amount_mode"] == "fixed" and c.get("amount"):
            amt = int(c["amount"])
        elif c["amount_mode"] == "price_plus_costs":
            amt = f.price
            for _ in range(3):   # ローン関連費用が借入額で変わるので数回まわして落ち着かせる
                r0 = compute(f, s, LoanPlan(amt), cd, hd, today=today, teiren=teiren)   # noqa
                amt = floor100k(f.price + r0.total_must + r0.total_loan)
        else:
            amt = floor100k(f.price * float(c["ratio"]))
        if amt > 0:
            loan = LoanPlan(amt, flat35=(c["type"] == "zenki_kotei" or (c.get("compare") and c["show"])))
    r = compute(f, s, loan, cd, hd, today=today, teiren=teiren)
    sim = simulate(loan.amount, c) if (loan and c["show"]) else None
    html = build_html(r, f, loan, customer=customer, property_name=property_name, address=f.address or "",
                      made=today, contract_day=cd, handover_day=hd, company=band or {},
                      font_dir=font_dir or os.path.join(HERE, "fonts"), sim=sim,
                      dates_tentative=(contract_day is None, handover_day is None))
    return {"html": html, "staff_notes": r.staff_notes, "result": r, "simulation": sim, "loan_conditions": c,
            "teiren_judge": r.teiren_judge, "teiren_applied": r.teiren_applied}
