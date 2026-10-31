"""購入諸費用の概算書（お客様向け A4 1枚）の HTML を作る。PDF化は render_pdf.js（Chromium）。"""
from __future__ import annotations

import html
from datetime import date

from costs_sale import Result, SaleFacts, LoanPlan

TYPE_JA = {"mansion": "中古マンション", "house": "中古一戸建て", "land": "土地"}
OTHERS = {
    "mansion": ["引越し費用", "リフォーム・クリーニング費用", "家具・家電・カーテンなど"],
    "house": ["引越し費用", "リフォーム・クリーニング費用", "家具・家電・カーテンなど", "建物の状況調査（インスペクション）"],
    "land": ["建物の建築費用・設計費用", "地盤調査・地盤改良", "測量・境界確定の費用", "解体費用（古家がある場合）"],
}


def yen(n: int) -> str:
    return f"{n:,}円"


def man(n: int) -> str:
    if n % 10000 == 0:
        return f"{n // 10000:,}万円"
    return yen(n)


def jdate(d: date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


def amount_cell(line) -> str:
    if line.amount is None:
        return '<span class="chk">要確認</span>'
    if line.kind == "estimate" and line.amount == 0 and line.group == "later":
        return "0円の見込み"
    if line.kind == "estimate":
        return f'<span class="yaku">約</span>{yen(line.amount)}'
    return yen(line.amount)


def rows(lines) -> str:
    out = []
    for l in lines:
        out.append(f'<tr><th>{html.escape(l.name)}</th><td class="amt">{amount_cell(l)}</td>'
                   f'<td class="note">{html.escape(l.note)}</td></tr>')
    return "\n".join(out)


def build_html(r: Result, f: SaleFacts, loan: LoanPlan | None, *, customer: str, property_name: str,
               address: str, made: date, contract_day: date, handover_day: date, company: dict,
               font_dir: str, sim: list | None = None, dates_tentative: tuple = (False, True)) -> str:
    must = [l for l in r.lines if l.group == "must"]
    lns = [l for l in r.lines if l.group == "loan"]
    later = [l for l in r.lines if l.group == "later"]
    shohiyou = r.total_must + r.total_loan
    total = f.price + shohiyou
    self_fund = total - (loan.amount if loan else 0)
    has_check = any(l.amount is None for l in r.lines if l.group in ("must", "loan"))
    spec = [TYPE_JA[f.property_type]]
    if f.floor_area:
        spec.append(("専有面積" if f.property_type == "mansion" else "建物面積") + f" {f.floor_area:.2f}㎡")
    if f.land_area and f.property_type != "mansion":
        spec.append(f"土地面積 {f.land_area:.2f}㎡")
    if f.built and not f.is_new:
        spec.append(f"{f.built.year}年{f.built.month}月築")
    notes = list(dict.fromkeys(r.notes))
    notes += [
        "「約」が付いた金額は目安です。税金は評価額や軽減措置の要件、ローン関連は金融機関やお客様の条件によって変わります。",
        "正式な金額は、ご契約・お引渡しの前にあらためてご案内します。",
    ]
    if f.property_type != "land":
        notes.insert(-1, "住宅ローン控除などの税の優遇が使えるかどうかは、要件の確認が必要です。")
    loan_block = ""
    if lns:
        loan_block = f"""
  <h2>住宅ローンをご利用の場合にかかる費用</h2>
  <table class="t"><colgroup><col class="c1"><col class="c2"><col class="c3"></colgroup>
  {rows(lns)}
  <tr class="sub"><th>小計</th><td class="amt"><span class="yaku">約</span>{yen(r.total_loan)}</td><td class="note"></td></tr>
  </table>"""
    later_rows = rows(later)
    sim_block = ""
    if sim:
        trs = "".join(
            f'<tr{" class=\'sel\'" if (x["selected"] and len(sim) > 1) else ""}><th>{html.escape(x["label"])}</th><td>年{x["rate"]:g}%</td>'
            f'<td class="amt"><span class="yaku">約</span>{yen(x["monthly"])}</td><td class="amt">約{yen(x["yearly"])}</td>'
            f'<td class="amt">約{yen(x["total"])}</td></tr>' for x in sim)
        sim_block = f"""
  <h2>住宅ローンシミュレーション（目安）</h2>
  <p class="simcond">借入額 {man(sim[0]["amount"])}・返済期間 {sim[0]["years"]}年・元利均等返済・ボーナス返済なし</p>
  <table class="t sim"><colgroup><col style="width:31%"><col style="width:11%"><col style="width:19%"><col style="width:19%"><col style="width:20%"></colgroup><tr class="hd"><th>金利タイプ</th><th>金利</th><th>毎月のご返済</th><th>年間のご返済</th><th>総返済額</th></tr>{trs}</table>"""
        types = {x["type"] for x in sim}
        msg = "ローンのご返済額は、上記の金利が返済期間中ずっと変わらない場合の目安です"
        if "hendo" in types:
            msg += "。変動金利は、今後の金利の動きでご返済額が変わります"
        if "kotei_kikan" in types:
            msg += "。固定期間選択は、固定期間が終わった後の金利で変わります"
        notes.insert(-1, msg + "。実際の金利は、金融機関の審査や商品で決まります。")
    others = "、".join(OTHERS[f.property_type])
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<style>
@font-face {{ font-family: "NotoJP"; font-weight: 400; src: url("file://{font_dir}/NotoSansJP-Regular.ttf"); }}
@font-face {{ font-family: "NotoJP"; font-weight: 700; src: url("file://{font_dir}/NotoSansJP-Bold.ttf"); }}
@page {{ size: A4; margin: 0; }}
:root {{ --blue: #1A56A8; --pale: #EAF1FB; --line: #9DB6DA; --ink: #1d2433; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; background: #fff; }}
body {{ font-family: "NotoJP", sans-serif; color: var(--ink); font-size: 9.6pt; line-height: 1.5;
       word-break: auto-phrase; line-break: strict; }}
.page {{ width: 210mm; height: 297mm; padding: 11mm 13mm 0 13mm; position: relative; }}
.head {{ display: flex; justify-content: space-between; align-items: flex-end; border-bottom: 2.2pt solid var(--blue); padding-bottom: 2mm; }}
h1 {{ margin: 0; font-size: 18pt; color: var(--blue); letter-spacing: .08em; font-weight: 700; }}
.made {{ font-size: 8.5pt; text-align: right; }}
.to {{ margin: 3mm 0 1mm; font-size: 12pt; font-weight: 700; }}
.to small {{ font-size: 9pt; font-weight: 400; margin-left: 4mm; }}
.prop {{ display: grid; grid-template-columns: 1fr auto; gap: 1mm 4mm; background: var(--pale); border: .6pt solid var(--line); padding: 2.2mm 3.5mm; border-radius: 1.5mm; }}
.prop .name {{ font-weight: 700; font-size: 11pt; }}
.prop .price {{ grid-row: span 2; align-self: center; text-align: right; }}
.prop .price b {{ font-size: 16pt; color: var(--blue); }}
.prop .sub {{ font-size: 8.6pt; }}
.cond {{ font-size: 8.4pt; margin: 1.6mm 0 0; }}
h2 {{ font-size: 10.5pt; margin: 3.4mm 0 1.2mm; padding-left: 2.4mm; border-left: 3pt solid var(--blue); font-weight: 700; }}
table.t {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
col.c1 {{ width: 30%; }} col.c2 {{ width: 20%; }} col.c3 {{ width: 50%; }}
.t th, .t td {{ border-bottom: .5pt solid var(--line); padding: 1.15mm 2mm; vertical-align: middle; }}
.t th {{ text-align: left; font-weight: 400; }}
.t td.amt {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; font-size: 10pt; }}
.t td.note {{ font-size: 7.8pt; color: #3c4658; }}
.t tr.sub th, .t tr.sub td {{ background: var(--pale); font-weight: 700; border-bottom: .8pt solid var(--blue); }}
.yaku {{ font-size: 8pt; margin-right: .6mm; }}
.chk {{ font-size: 8.6pt; border: .6pt solid var(--ink); padding: 0 1.4mm; border-radius: 1mm; }}
.sum {{ margin-top: 3.6mm; border: 1.2pt solid var(--blue); border-radius: 1.5mm; overflow: hidden; }}
.sum .row {{ display: grid; grid-template-columns: 1fr auto; padding: 1.6mm 4mm; border-top: .5pt solid var(--line); }}
.sum .row:first-child {{ border-top: 0; background: var(--blue); color: #fff; }}
.sum .row b {{ font-size: 12.5pt; }}
.sum .row:first-child b {{ font-size: 14pt; }}
.sum .row span.l small {{ font-size: 7.8pt; margin-left: 2mm; }}
.others {{ font-size: 8.4pt; margin: 1.2mm 0 0; }}
ul.notes {{ margin: 2.6mm 0 0; padding-left: 4.5mm; font-size: 7.8pt; color: #3c4658; }}
ul.notes li {{ margin: .3mm 0; }}
.band {{ position: absolute; left: 0; right: 0; bottom: 0; background: var(--blue); color: #fff; padding: 3mm 13mm; display: flex; justify-content: space-between; align-items: center; }}
.band .co {{ font-size: 12pt; font-weight: 700; }}
.simcond {{ font-size: 8.2pt; margin: 0 0 1mm; }}
table.sim th, table.sim td {{ padding: .9mm 2mm; font-size: 8.8pt; }}
table.sim tr.hd th {{ background: var(--pale); font-weight: 700; font-size: 8pt; text-align: center; }}
table.sim tr.sel th, table.sim tr.sel td {{ font-weight: 700; }}
.nw {{ white-space: nowrap; display: inline-block; }}
.band .meta {{ font-size: 7.8pt; line-height: 1.55; }}
.band > div:first-child {{ min-width: 0; flex: 1 1 auto; padding-right: 5mm; }}
.band .tan {{ text-align: right; font-size: 9pt; line-height: 1.5; white-space: nowrap; flex: 0 0 auto; }}
.band .tan b {{ font-size: 11pt; }}
</style></head>
<body><div class="page"><div class="content">
  <div class="head"><h1>購入諸費用の概算書</h1>
    <div class="made">作成日 {jdate(made)}</div></div>
  <div class="to">{html.escape(customer)} 様<small>ご購入にかかる費用の目安をまとめました。</small></div>
  <div class="prop">
    <div class="name">{html.escape(property_name)}</div>
    <div class="price">物件価格 <b>{man(f.price)}</b></div>
    <div class="sub">{html.escape(address)}　{"　".join(spec)}</div>
  </div>
  <p class="cond">計算の前提：ご契約 {jdate(contract_day)}ごろ{"（仮）" if dates_tentative[0] else ""}、決済・お引渡し {jdate(handover_day)}{"（仮）" if dates_tentative[1] else ""}{("、住宅ローン " + man(loan.amount)) if loan else "、住宅ローンなし"}</p>

  <h2>お引渡しまでに必ずかかる費用</h2>
  <table class="t"><colgroup><col class="c1"><col class="c2"><col class="c3"></colgroup>
  {rows(must)}
  <tr class="sub"><th>小計</th><td class="amt"><span class="yaku">約</span>{yen(r.total_must)}</td><td class="note">{"「要確認」の項目は含みません" if any(l.amount is None for l in must) else ""}</td></tr>
  </table>
  {loan_block}

  <h2>お引渡しの後にかかる費用（参考）</h2>
  <table class="t"><colgroup><col class="c1"><col class="c2"><col class="c3"></colgroup>
  {later_rows}
  </table>
  <p class="others">このほか、必要に応じて{html.escape(others)}がかかります。</p>

  <div class="sum">
    <div class="row"><span class="l">諸費用の合計（目安）<small>{"「要確認」の項目を除く" if has_check else "参考の費用を除く"}</small></span><b>約{yen(shohiyou)}</b></div>
    <div class="row"><span class="l">物件価格と諸費用の合計</span><b>約{yen(total)}</b></div>
    <div class="row"><span class="l">ご用意いただく自己資金の目安<small>{"住宅ローン " + man(loan.amount) + " を除いた額" if loan else ""}</small></span><b>約{yen(self_fund)}</b></div>
  </div>

  {sim_block}
  <ul class="notes">{"".join(f"<li>{html.escape(n)}</li>" for n in notes)}</ul>

  </div>
  <div class="band">
    <div><div class="co">{html.escape(company.get("name", ""))}</div>
      <div class="meta">{"　".join(f'<span class="nw">{html.escape(x)}</span>' for x in company.get("license", "").split("　") if x)}<br>{html.escape(company.get("address", ""))}　TEL {html.escape(company.get("tel", ""))}</div></div>
    <div class="tan">担当 <b>{html.escape(company.get("staff", ""))}</b>{("<br>携帯 " + html.escape(company["mobile"])) if company.get("mobile") else ""}</div>
  </div>
</div>
<script>
// A4 1枚に収める：はみ出すときだけ縮小（最小70%）
(document.fonts ? document.fonts.ready : Promise.resolve()).then(() => {{
  const pg = document.querySelector('.page'), c = document.querySelector('.content'), b = document.querySelector('.band');
  const avail = pg.clientHeight - b.offsetHeight - c.offsetTop - 4;
  const h = c.scrollHeight;
  if (h > avail) {{ const z = Math.max(0.7, avail / h); c.style.zoom = z; }}   // 幅はそのまま（width を足すと右にはみ出して全体が縮む）
  document.body.dataset.fitted = '1';
}});
</script>
</body></html>"""
