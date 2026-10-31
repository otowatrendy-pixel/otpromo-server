"""帯の外（備考欄など）に書かれた、お客様に見せる必要のない元付業者向けの情報を白で消す。

AIは「どの文字を消すか」（図面に書いてあるとおりの文字）を答え、
文字の位置は図面の画像を文字認識（RapidOCR＋PP-OCRv5の日本語対応モデル）して正確に探す。
AIの答えた位置の枠は不正確なことがあるため使わない。

消すもの：
- 元付業者の電話・FAX番号を含む行（支店・営業所の番号も、番号が同じなら見つかる）
- AIが「帯の外の業者向け情報」に挙げた文字（広告料・AD・報酬の配分・鍵の場所・内見の連絡方法など）
- 文字認識の結果に、広告料・AD・配分・報酬・鍵の場所などの言葉が見つかった部分（AIの見落としの保険）
"""
import re
import unicodedata
from difflib import SequenceMatcher

import numpy as np
from PIL import Image

_ocr = None


def _engine():
    global _ocr
    if _ocr is None:
        from pathlib import Path
        from rapidocr_onnxruntime import RapidOCR
        # 日本語も読める PP-OCRv5 のモデル（models/ppocrv5、Apache-2.0）。なければ付属の中国語モデルで読む
        m = Path(__file__).resolve().parent / "models" / "ppocrv5"
        if (m / "rec.onnx").exists():
            _ocr = RapidOCR(det_model_path=str(m / "det.onnx"), rec_model_path=str(m / "rec.onnx"),
                            rec_keys_path=str(m / "dict.txt"))
        else:
            _ocr = RapidOCR()
    return _ocr


def _norm(s: str) -> str:
    """比べるための形にする（全角半角をそろえ、空白・かっこ・区切り記号を外す）。"""
    s = unicodedata.normalize("NFKC", s or "").upper()
    return re.sub(r"[\s\[\]【】()（）「」『』<>〈〉:：;；,，、。・/／|｜\-ー－‐]", "", s)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", unicodedata.normalize("NFKC", s or ""))


# 文字認識は中国語の字形で返すことがあるため、日本語と中国語の両方の字形で探す
_KEYWORDS = re.compile(
    r"([広广去]?告料|ＡＤ|(?<![A-Z])AD(?![A-Z])|業者間|业者间|配分|报酬|報酬|キーボックス|"
    r"(?:鍵|键)(?!交換|交换))")
# 「内見」は、見出し（【内見】）か、連絡の方法が書いてある行だけ（「内見後に仕様が変わる場合」などのお客様向けの注意は残す）
_NAIKEN = re.compile(r"[【\[［(（]内[見见兄觅][】\]］)）]|内[見见兄觅].*(FA|TEL|電|电|話|话|連絡|连络|予約|预约|依頼|依赖|申込|キー|鍵|键|書式)|"
                     r"(FAX|TEL|電話|电话|連絡|连络|予約|依頼|申込|キー|鍵|键).*内[見见兄觅]")


# 売買版：消す言葉のすぐ後ろに続く番号（「レインズ 物件番号：100123456789」の番号の部分）
_TAIL = re.compile(r"(\s*[:：]?\s*(物件番号|登録番号|番号|No\.?|NO\.?)?\s*[:：]?\s*[0-9A-Za-z\-]{4,})?")


def _width(ch: str) -> float:
    """文字の横幅の目安（半角は全角のおよそ半分）。"""
    return 0.55 if ord(ch) < 0x2E80 else 1.0


def _span_box(box, text, i0, i1, pad):
    """認識した1行の枠のうち、i0〜i1文字目にあたる横の範囲を返す。"""
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
    w = [_width(c) for c in text] or [1.0]
    total = sum(w)
    a = left + (right - left) * sum(w[:i0]) / total
    b = left + (right - left) * sum(w[:i1]) / total
    return (a - pad, top, b + pad, bottom)


def _best_window(short: str, long: str):
    """long の中で short にいちばん似ている部分の (似ている度合い, 始め, 終わり)。"""
    n = len(short)
    if n == 0 or not long:
        return 0.0, 0, 0
    if n >= len(long):
        return SequenceMatcher(None, short, long).ratio(), 0, len(long)
    best = (0.0, 0, n)
    for i in range(0, len(long) - n + 1):
        r = SequenceMatcher(None, short, long[i:i + n]).ratio()
        if r > best[0]:
            best = (r, i, i + n)
    return best


def _norm_index(text: str):
    """正規化前の文字位置と正規化後の文字位置の対応表。"""
    idx = []
    for i, c in enumerate(text):
        idx.extend([i] * len(_norm(c)))
    return idx


def find(page: Image.Image, facts, texts, band_top: float, extra=None, protect=None):
    """消す枠（ピクセル）と、その理由の一覧を返す。帯より上の行だけを対象にする。
    extra：言葉の保険に足す正規表現（売買版の「分かれ」「片手」「レインズ」など）。賃貸版は None。
    protect：この正規表現に当たる行は、何があっても消さない（売買版の告知事項・再建築不可など）。賃貸版は None。"""
    img = page.convert("RGB")
    scale = 1.0
    if max(img.size) > 2400:
        scale = 2400 / max(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)))
    out, _ = _engine()(np.asarray(img))
    limit = band_top * page.height
    pad = max(page.width, page.height) * 0.003
    phones = {d for d in (_digits(facts.元付業者_電話), _digits(facts.元付業者_FAX)) if len(d) >= 9}
    wanted = [_norm(t) for t in texts or [] if len(_norm(t)) >= 2]
    multi = extra is not None
    boxes, notes, lines = [], [], []
    for box, text, _score in out or []:
        box = [[x / scale, y / scale] for x, y in box]
        if min(p[1] for p in box) >= limit:
            continue
        whole = tuple(_span_box(box, text, 0, len(text), pad))
        lines.append(whole)
        nt = _norm(text)
        if protect is not None and protect.search(unicodedata.normalize("NFKC", text)):
            continue
        # 1) 元付業者の電話・FAX番号を含む行は、行ごと消す
        d = _digits(text)
        if any(p in d for p in phones):
            boxes.append(whole)
            notes.append(text)
            continue
        # 2) AIが挙げた文字と同じ行・部分を消す
        hit = False
        for w in wanted:
            if len(nt) <= len(w):
                # 認識した行がAIの文字の一部（長い文が複数行に分かれた場合など）
                # 短い行（数字だけの欄など）は偶然似てしまうので、そのまま含まれているときだけにする
                # （「入居可能日」のような見出しが、AIの挙げた長い文の一部と重なって消えるのを防ぐ）
                r = (1.0 if nt == w else 0.0) if len(nt) < 8 else _best_window(nt, w)[0]
                if r >= 0.75 and len(nt) >= 3:
                    boxes.append(whole)
                    notes.append(text)
                    hit = True
                    break
            elif len(w) >= len(nt) * 0.6:
                if SequenceMatcher(None, w, nt).ratio() >= 0.6:
                    boxes.append(whole)
                    notes.append(text)
                    hit = True
                    break
            elif len(w) >= 4:  # 2〜3文字の短い文字は、偶然ほかの行の一部と合ってしまうので使わない（言葉の保険で拾う）
                r, i0, i1 = _best_window(w, nt)
                if r >= 0.6:
                    idx = _norm_index(text)
                    a, b = idx[i0], idx[min(i1, len(idx)) - 1] + 1
                    boxes.append(_span_box(box, text, a, b, pad))
                    notes.append(text[a:b])
                    hit = True
                    if multi:  # 売買版：1行に業者向けの情報が並ぶことが多いので、ほかの文字も探す
                        continue
                    break
        if hit and not multi:
            continue
        if multi:
            # 売買版：言葉そのもの（とすぐ後ろの番号）だけを消す。同じ行のお客様向けの記載は残す
            t = unicodedata.normalize("NFKC", text)
            for m in extra.finditer(t):
                tail = _TAIL.match(t, m.end())
                a, b = m.start(), tail.end() if tail else m.end()
                boxes.append(_span_box(box, text, a, b, pad))
                notes.append(text[a:b])
                hit = True
        if hit:
            continue
        # 3) 言葉で見つかったものは、その言葉から次の区切りまでを消す
        m = _KEYWORDS.search(unicodedata.normalize("NFKC", text)) or _NAIKEN.search(unicodedata.normalize("NFKC", text)) \
            or (extra.search(unicodedata.normalize("NFKC", text)) if extra is not None else None)
        if m:
            t = unicodedata.normalize("NFKC", text)
            end = re.search(r"[、,，/／]|\s{2,}", t[m.end():])
            b = m.end() + end.start() if end else len(t)
            # 区切り（、など）から次の区切りまでの、その言葉を含むひとまとまりを消す
            a = max([t.rfind(c, 0, m.start()) for c in "、,，/／"] + [-1]) + 1
            boxes.append(_span_box(box, text, a, b, pad))
            notes.append(text[a:b])
    def _inside(l, b):
        cx, cy = (l[0] + l[2]) / 2, (l[1] + l[3]) / 2
        return b[0] <= cx <= b[2] and b[1] <= cy <= b[3]
    kept = [l for l in lines if not any(_inside(l, b) for b in boxes)]
    return _fill_between(boxes, kept, lines, limit), notes


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _fill_between(boxes, kept, lines, limit):
    """同じ列で上下に並んだ消す行のあいだ（または消す行と帯のあいだ）に、
    文字認識が読めなかった細い行が挟まっていれば、そこも消す。
    （【内見】の見出しと連絡先の行のあいだにある「内見依頼書をFAXのうえ…」のような行）
    あいだに消さない行（保証料の説明など）があるときは、何もしない。"""
    out = list(boxes)
    rows = sorted(boxes, key=lambda b: b[1])
    for i, a in enumerate(rows):
        h = a[3] - a[1]
        below = [b for b in rows[i + 1:] if abs(a[0] - b[0]) <= h * 1.5 and b[1] > (a[1] + a[3]) / 2]
        # 帯は図面の区切りに合わせて少し下にずれることがあるので、帯の上端より少し下まで消す
        b = below[0] if below else (a[0], limit + h * 1.5, a[2], limit + h * 3)
        if b[1] - a[3] > h * (2.5 if below else 4):
            continue
        # 読めなかった行の幅はわからないので、同じ列でいちばん長い行の右端まで消す
        right = max([a[2], b[2]] + [l[2] for l in lines if abs(l[0] - a[0]) <= h * 1.5])
        # 上の行の真ん中から下の行の真ん中まで（行どうしの枠が重なっていて、あいだの行が読めなかった場合も消せる）
        area = (min(a[0], b[0]), (a[1] + a[3]) / 2, right, (b[1] + b[3]) / 2 if below else b[1])
        if any(area[1] <= (k[1] + k[3]) / 2 <= area[3] and area[0] <= (k[0] + k[2]) / 2 <= area[2] for k in kept):
            continue
        out.append(area)
    return out


def _erase(arr, x0, y0, x1, y1):
    """枠の中の文字を、まわりの地の色で塗る。表の罫線（長い縦線・横線）は残す。"""
    import cv2
    H, W = arr.shape[:2]
    x0, y0, x1, y1 = max(0, int(x0)), max(0, int(y0)), min(W, int(x1 + 1)), min(H, int(y1 + 1))
    if x1 <= x0 or y1 <= y0:
        return
    # 罫線は枠より広い範囲で探す（枠の中だけだと、文字の縦の線と見分けにくい）
    m = 60
    X0, Y0, X1, Y1 = max(0, x0 - m), max(0, y0 - m), min(W, x1 + m), min(H, y1 + m)
    gray = cv2.cvtColor(arr[Y0:Y1, X0:X1], cv2.COLOR_RGB2GRAY)
    box = arr[y0:y1, x0:x1]
    bg = np.percentile(box.reshape(-1, 3), 90, axis=0).astype(np.uint8)
    # 紺色などの濃い地（表の見出しのセル）は、業者向けの情報が書かれる所ではないので塗らない
    if bg.mean() < 150:
        return
    ink = (gray < int(np.percentile(gray, 90)) - 40).astype(np.uint8)
    hl = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (90, 1)))
    vl = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 70)))
    lines = cv2.dilate(hl | vl, np.ones((3, 3), np.uint8))[y0 - Y0:y1 - Y0, x0 - X0:x1 - X0].astype(bool)
    keep = box[lines].copy()
    box[:] = bg
    box[lines] = keep


def apply(page: Image.Image, result, extra=None, protect=None):
    """帯の外の業者向け情報を白で消した画像と、消した文字の一覧を返す。extra・protect は find と同じ（売買版用）。"""
    b = result.帯
    band_top = b.上端 if b.見つかった else 1.0
    try:
        boxes, notes = find(page, result.事実, result.帯の外の業者向け情報, band_top, extra, protect)
    except Exception as ex:  # 文字認識が使えないときは消さずに続ける
        return page, [f"文字認識に失敗：{ex}"]
    if not boxes:
        return page, []
    arr = np.asarray(page.convert("RGB")).copy()
    for x0, y0, x1, y1 in boxes:
        # 用紙の4分の1を超える大きな枠は、読み違いとみなして消さない
        if (x1 - x0) * (y1 - y0) > page.width * page.height * 0.25:
            continue
        _erase(arr, x0, y0, x1, y1)
    out = Image.fromarray(arr)
    return out, notes


def ask(image: Image.Image, request: str, model: str = "claude-sonnet-5-5", effort: str = "low"):
    """利用者の「ここを消して」の依頼と帯替え図面から、消す文字の一覧をAIに答えてもらう。
    返り値は (消す文字の一覧, できないことの説明 または None, usage, モデル名)。"""
    import base64
    import io
    import json
    import os
    import anthropic
    from prompt import FIX_SYSTEM
    img = image.convert("RGB")
    img.thumbnail((2000, 2000))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    key = os.environ.get("MAISOKU_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=key, base_url="https://api.anthropic.com")
    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=FIX_SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(buf.getvalue()).decode()}},
            {"type": "text", "text": "依頼：" + request},
        ]}],
        output_config={"effort": effort},
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    obj, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
    texts = [t for t in obj.get("消す文字") or [] if isinstance(t, str) and t.strip()]
    return texts, obj.get("できないこと"), resp.usage, resp.model
