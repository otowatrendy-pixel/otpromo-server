"""売買版「内見依頼書」（FAX用）。

賃貸版の内見申込書（naiken_template.html / render.naiken_html）をほぼそのまま使う売買用のテンプレートです。
まだLINEの会話（naiken_bot.py）にはつないでいません。賃貸版の動きには一切さわらない独立したファイルです。

方針（2026-10-31 本人）：内見依頼書を送る前に物件確認は済んでいる。LINEのやりとりで疲れさせないよう、記入は最低限。
- 物件名・号室・所在地・価格・物件番号・宛先は、販売図面の読み取り（SaleFacts）から自動で入れる
- LINEで聞くのは内見の希望日時だけ（宛先FAXが読み取れなかったときだけFAX番号も聞く。賃貸版と同じ）
- 物件確認の項目・元付業者のご回答欄・税や評価額の欄は付けない
- お客様の氏名や資金計画は載せない（誤送信のときの被害を小さくするため）

見本を作る：  python app/baibai_naiken.py 出力フォルダ
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
TEMPLATE = HERE / "baibai_naiken_template.html"
WEEK = "月火水木金土日"
BLANK_WISH = "　　月　　日（　　）　　時　　分頃"


def sent_date(now=None):
    t = now or datetime.now(timezone(timedelta(hours=9)))  # 日付は日本時間
    return f"{t.year}年{t.month}月{t.day}日（{WEEK[t.weekday()]}）"


def from_sale_facts(facts):
    """売買の読み取り結果（sale_schema.SaleFacts、またはその dict）から、宛先と物件の下書きを作る。

    LINEで聞かずに埋まる項目はすべてここで入れる。元付業者_FAX が空なら空のまま（電話番号で代用しない。賃貸版と同じ決まり）。
    check には、送る前に利用者へ確かめる宛先項目（空欄・読み取りに自信がない）を入れる。
    """
    f = facts.model_dump() if hasattr(facts, "model_dump") else dict(facts or {})
    price = f.get("価格") or {}
    shaky = set(f.get("読み取りに自信がない項目") or [])
    check = [k for k, field in (("company", "元付業者"), ("fax", "元付業者_FAX")) if field in shaky or not f.get(field)]
    room = re.sub(r"\s*号室?$", "", (f.get("号室") or "").strip()) or None
    return {
        "to": {"company": f.get("元付業者") or "", "branch": "", "person": "", "fax": f.get("元付業者_FAX") or "",
               "tel": f.get("元付業者_電話") or "", "check": check},
        "prop": {"name": f.get("物件名") or "", "room": room, "address": f.get("所在地") or "",
                 "price": price.get("記載") or "", "number": f.get("物件番号") or ""},
        "wishes": [],
    }


def html(to, me, prop, wishes=None, now=None, qr_svg=None, **_old):
    """内見依頼書のHTMLを作る。

    to   : {"company", "branch", "person", "fax"}（元付業者）
    me   : naiken.me_from_company() と同じ形（自社・担当者の名刺情報）
    prop : {"name", "room", "address", "price", "number"}（販売図面から自動で入る）
    wishes: 希望日時の文字列の並び（最大3つ。LINEで聞くのはここだけ）。空文字は手書き用の空欄。None なら空欄2行
    _old : 以前の版の guests・note・ad・tax は受け取っても使わない（つなぎ替えの途中で落ちないように）
    """
    from jinja2 import Template
    from markupsafe import Markup
    import render

    if qr_svg is None:
        qr = HERE / "static" / "naiken-qr.svg"  # 賃貸版と同じ「内見FAX」経路のQR。売買版のLINEが決まったら差し替える
        qr_svg = Markup(qr.read_text(encoding="utf-8")) if qr.exists() else ""
    data = {
        "sent_date": sent_date(now), "to": to or {}, "me": me or {}, "prop": dict(prop or {}),
        "wishes": [w or BLANK_WISH for w in (wishes or [])][:3] or [BLANK_WISH, BLANK_WISH], "qr_svg": qr_svg,
    }
    tpl = TEMPLATE.read_text(encoding="utf-8")
    # 書体はネットから読まず、同梱のものを使う（賃貸版と同じ）
    tpl = re.sub(r'<link href="https://fonts\.googleapis\.com[^>]*>', "", tpl)
    css = render.font_css({"書体": "ゴシック"})[0] + render.font_css({"書体": "明朝"})[0]
    return Template(tpl, autoescape=True).render(**data).replace("<style>", "<style>\n" + css, 1)


def pdf(out: Path, **kw):
    import render
    render.to_pdf(html(**kw), out)
    return out


# ---------- 見本（架空のデータだけを使う） ----------

ME = {"company": "サンプル不動産株式会社", "branch": "見本駅前店", "role": "売買営業課　宅地建物取引士",
      "name": "見本 太郎", "roman": "TARO MIHON", "zip": "000-0000", "address": "東京都○○区見本町1丁目2番3号",
      "tel": "03-0000-1234", "fax": "03-0000-1235", "mobile": "090-0000-0000", "email": "mihon@example.jp",
      "license": "東京都知事（1）第00000号"}

SAMPLES = {
    "mihon_mansion": dict(
        to={"company": "見本住宅販売株式会社", "branch": "", "person": "", "fax": "03-0000-5678"},
        prop={"name": "パークメゾン見本", "room": "503", "address": "東京都○○区見本町4丁目5番6号", "price": "4,280万円"},
        wishes=["11月7日（土）14時00分頃", "11月8日（日）10時00分頃", "11月8日（日）午後"]),
    "mihon_tochi": dict(
        to={"company": "見本ホーム株式会社", "branch": "見本店", "person": "見本", "fax": "03-0000-9012"},
        prop={"name": "○○区見本町 売地", "address": "東京都○○区見本町7丁目8番", "price": "3,580万円",
              "number": "000000000000"},
        wishes=["11月10日（火）11時00分頃", "11月12日（木）午前"]),
    "mihon_kuuran": dict(
        to={"company": "", "branch": "", "person": "", "fax": ""},
        prop={"name": "", "address": "", "price": ""}, wishes=["", ""]),
}


def make_samples(outdir: Path):
    import render
    outdir.mkdir(parents=True, exist_ok=True)
    groups = {outdir / f"{k}.pdf": [html(me=ME, **kw)] for k, kw in SAMPLES.items()}
    render.to_pdfs(groups)
    return list(groups)


if __name__ == "__main__":
    for p in make_samples(Path(sys.argv[1] if len(sys.argv) > 1 else "baibai_naiken_mihon")):
        print(p)
