"""LINE（sale_bot.py）から概算書を作るときの入口。

    from sale_estimate import make_estimate
    out = make_estimate(company, facts_dict, evals=None, customer="見本 太郎", property_name="メゾン見本 302号室",
                        band={...}, contract_day=None, handover_day=None, loan_amount=None, today=None)
    out["html"]         … お客様向けA4 1枚のHTML（render_pdf と同じChromiumでPDF化）
    out["staff_notes"]  … スタッフ向けの確認事項（お客様向けPDFには入れない。LINEの文面に添える）
    out["result"]       … 計算結果（Result）

company["売買設定"]      … CompanySaleSettings と同じキー名（知らないキーは無視）
company["売買ローン目安"] … {"show": bool, "rate": 年利(%), "years": 年数, "ratio": 借入の割合(省略時1.0)}
evals                    … スタッフがLINEで送った値 {"land_eval","bldg_eval","annual_property_tax",
                             "rosenka_per_m2","kotei_rosenka_per_m2"}（あるものだけ）
契約日・引渡日が None なら、作成日と作成日の60日後で仮置き（概算書に「（仮）」と出る）。
"""
from __future__ import annotations

import dataclasses
import os
from datetime import date, timedelta
from typing import Optional

from costs_sale import CompanySaleSettings, LoanPlan, SaleFacts, compute
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


def make_estimate(company: dict, facts: dict, evals: Optional[dict] = None, *, customer: str = "",
                  property_name: str = "", band: Optional[dict] = None, contract_day: Optional[date] = None,
                  handover_day: Optional[date] = None, loan_amount: Optional[int] = None,
                  today: Optional[date] = None, font_dir: Optional[str] = None) -> dict:
    today = today or date.today()
    f = SaleFacts(**facts)
    if evals:
        f = dataclasses.replace(f, **{k: v for k, v in evals.items() if k in EVAL_KEYS and v is not None})
    s = settings_from_company(company)
    lm = company.get("売買ローン目安") or {}
    loan = None
    if lm.get("show", True):
        amt = loan_amount if loan_amount is not None else int(f.price * float(lm.get("ratio", 1.0)) // 100_000 * 100_000)
        if amt > 0:
            loan = LoanPlan(amt)
    cd = contract_day or today
    hd = handover_day or (today + timedelta(days=60))
    r = compute(f, s, loan, cd, hd, today=today)
    monthly = None
    if loan and lm.get("rate") and lm.get("years"):
        monthly = {"amount": monthly_payment(loan.amount, float(lm["rate"]), int(lm["years"])),
                   "rate": float(lm["rate"]), "years": int(lm["years"])}
    html = build_html(r, f, loan, customer=customer, property_name=property_name, address=f.address or "",
                      made=today, contract_day=cd, handover_day=hd, company=band or {},
                      font_dir=font_dir or os.path.join(HERE, "fonts"), monthly=monthly,
                      dates_tentative=(contract_day is None, handover_day is None))
    return {"html": html, "staff_notes": r.staff_notes, "result": r, "monthly": monthly}
