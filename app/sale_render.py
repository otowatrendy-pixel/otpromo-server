"""売買版の資料のHTML（帯替え図面・スタッフへの注意・おすすめポイント・想定Q&A）。

色・書体・PDF化は賃貸版の render.py をそのまま使う。購入諸費用の概算書は別のファイルで作る。
お客様向け：① 帯替え図面（＋概算書）　社内向け：スタッフへの注意と物件の要点・② おすすめポイント・④ 想定Q&A
スタッフ向けの注意書き（render.NOTE）と業者向けの情報は、お客様向けには入れない。
"""
import base64
import html

import render

OSUSUME_NOTE = "お客様へ送る前に内容をご確認ください"

ALL_SECTIONS = ("obikae", "chui", "osusume", "qa")


def _row(label, value):
    return f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>" if value else ""


def summary_rows(f) -> str:
    """社内向けの物件の要点（図面にある項目だけ）。"""
    rows = [
        ("物件種別", f.物件種別 + ("（新築）" if f.新築 else "")),
        ("価格", (f.価格.記載 or "") + (f"（{f.価格_消費税}）" if f.価格_消費税 != "記載なし" else "")),
        ("管理費／修繕積立金", "／".join(x for x in (f.管理費.記載, f.修繕積立金.記載) if x)),
        ("土地の権利", f.土地権利 or ""),
        ("現況／引渡し", "／".join(x for x in (f.現況, f.引渡し) if x)),
        ("取引態様（元付）", f.取引態様 or "記載なし"),
        ("売主", f.売主 or ""),
        ("元付業者", " ".join(x for x in (f.元付業者, f.元付業者_電話 and f"TEL {f.元付業者_電話}",
                                       f.元付業者_FAX and f"FAX {f.元付業者_FAX}") if x)),
    ]
    return "".join(_row(k, v) for k, v in rows)


def build_html(r, obikae_png: bytes, company: dict, sections=ALL_SECTIONS, bw=False) -> str:
    e = html.escape
    f = r.事実
    name = " ".join(x for x in (f.物件名, f.号室 and f"{f.号室}号室") if x) or f.所在地 or "物件"
    img64 = base64.b64encode(obikae_png).decode()
    points = "".join(
        f"<li><b>{e(p.見出し)}</b><p>{e(p.説明)}</p><small>根拠：{e(p.根拠)}</small></li>" for p in r.おすすめポイント)
    qa = "".join(
        f"<dt>Q{i}. {e(q.質問)}{' <span class=chk>要確認</span>' if q.要確認 else ''}</dt><dd>{e(q.参考回答)}</dd>"
        for i, q in enumerate(r.想定QA, 1))
    chui = "".join(f"<li>{e(t)}</li>" for t in r.スタッフへの注意)
    if bw:
        company = {**company, "テーマ色": "#222222"}
    t = render.theme(company)
    fcss, family = render.font_css(company)
    parts = {
        # お客様向け：注意書き・業者向けの情報は入れない
        "obikae": f"""<section><h1>① 帯替え図面<small>{e(name)}</small></h1>
<img src="data:image/png;base64,{img64}"></section>""",
        "chui": f"""<section class=keep><h1>スタッフへの注意<small>{e(name)}</small></h1>
<ul class=chui>{chui}</ul>
<h2>物件の要点（販売図面より）</h2><table class=kv>{summary_rows(f)}</table>
<p class=note>{render.NOTE}</p></section>""",
        "osusume": f"""<section><h1>② おすすめポイント<small>{e(name)}</small></h1>
<p class=send>{e(OSUSUME_NOTE)}（数字や言葉は、販売図面の記載にもとづいています）</p><ol class=pt>{points}</ol></section>""",
        "qa": f"""<section><h1>④ 想定Q&amp;A<small>{e(name)}</small></h1><dl>{qa}</dl>
<p class=note>{render.NOTE}</p></section>""",
    }
    body = "\n".join(parts[k] for k in sections)
    return f"""<!doctype html><html lang=ja><meta charset=utf-8><style>
{fcss}
:root {{ --main: {t['main']}; --line: {t['line']}; --light: {t['light']}; --pale: {t['pale']}; --font: {family}; }}
@page {{ size: A4; margin: 14mm 14mm 16mm; }}
body {{ font-family: var(--font); color: #222; font-size: 10.5pt; line-height: 1.7;
  word-break: auto-phrase; line-break: strict; }}
h1 {{ color: var(--main); font-size: 16pt; border-bottom: 3px solid var(--main); padding-bottom: 4px; margin: 0 0 10px; }}
h1 small {{ font-size: 9pt; color: #555; margin-left: 8px; }}
h2 {{ color: var(--main); font-size: 11.5pt; margin: 14px 0 4px; }}
section {{ page-break-after: always; }}
section.keep {{ page-break-after: avoid; }}
section:last-child {{ page-break-after: auto; }}
img {{ display: block; max-width: 100%; max-height: 250mm; margin: 0 auto; border: 1px solid #ccc; }}
ol.pt li {{ margin-bottom: 10px; }} ol.pt p {{ margin: 2px 0; }} small {{ color: #666; }}
ul.chui {{ padding-left: 1.2em; }} ul.chui li {{ margin-bottom: 6px; background: var(--pale);
  border-left: 4px solid var(--main); padding: 4px 8px; list-style: none; margin-left: -1.2em; }}
table.kv {{ width: 100%; border-collapse: collapse; font-size: 9.5pt; }}
table.kv th {{ background: var(--light); text-align: left; width: 28%; border: 1px solid var(--line); padding: 3px 8px; }}
table.kv td {{ border: 1px solid var(--line); padding: 3px 8px; }}
dt {{ font-weight: bold; margin-top: 8px; }} dd {{ margin: 2px 0 0 1.2em; }}
.chk {{ background: var(--light); color: var(--main); font-size: 8pt; padding: 1px 6px; border-radius: 8px; }}
.send {{ background: var(--pale); border-left: 4px solid var(--main); padding: 4px 8px; font-size: 9.5pt; }}
.note {{ font-size: 8.5pt; color: #666; margin-top: 12px; }}
</style>
{body}</html>"""
