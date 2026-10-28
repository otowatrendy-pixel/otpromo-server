"""スマホで撮った募集図面の写真を、スキャンしたような図面に整える。

1. 用紙（または画面に映った図面）の四隅を見つける
2. 台形補正で、まっすぐな長方形に引き伸ばす（A4に近ければA4の比率に合わせる）
3. 影・照明のむら・モニターのモアレを和らげ、背景を白く、文字をくっきりさせる

図面の文字や絵を描き直すことはしない。画素の位置と明るさを整えるだけ。
四隅が見つからないときは、明るさの補正だけをして元の構図のまま返す。
"""
from typing import Optional, Tuple

import cv2
import numpy as np
from PIL import Image

A4 = 297 / 210
# 出来上がりの長辺のピクセル数（A4を約200dpi）
LONG_EDGE = 2339


def _order(pts: np.ndarray) -> np.ndarray:
    """四隅を 左上・右上・右下・左下 の順に並べる。"""
    pts = pts.reshape(4, 2).astype("float32")
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], dtype="float32")


def _candidate(gray: np.ndarray, thr: float):
    _, mask = cv2.threshold(gray, thr, 255, cv2.THRESH_BINARY)
    # 図面の中の罫線や写真で用紙が欠けないよう穴を埋め、細いはみ出しを削る
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    hull = cv2.convexHull(c)
    peri = cv2.arcLength(hull, True)
    for eps in (0.02, 0.03, 0.05):
        approx = cv2.approxPolyDP(hull, eps * peri, True)
        if len(approx) == 4:
            break
    else:
        return None
    area = cv2.contourArea(c)
    fit = area / max(1.0, cv2.contourArea(approx))
    return approx.reshape(4, 2).astype("float32"), area / gray.size, fit


def _refine(gray: np.ndarray, q: np.ndarray, radius: float = 0.07, paper_check: bool = True) -> np.ndarray:
    """四角形の各辺を、近くでいちばん「内側が明るく外側が暗い」直線に合わせ直す。
    画面の灰色の余白や影で、四角形が用紙より大きく／小さく取れたときの補正。"""
    q = _order(q)
    g = cv2.GaussianBlur(gray, (5, 5), 0).astype(np.float32)
    h, w = g.shape
    center = q.mean(0)
    # 用紙の白さの目安：四角形の中心寄りの明るい部分
    inner = np.zeros((h, w), np.uint8)
    cv2.fillConvexPoly(inner, ((q - center) * 0.7 + center).astype(np.int32), 1)
    paper = np.percentile(g[inner == 1], 75)
    R = max(2, int(max(h, w) * radius))
    offs = np.arange(-R, R + 1, max(1, R // 15))
    t = np.linspace(0.05, 0.95, 120)[:, None]
    lines = []
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        d = (b - a) / np.linalg.norm(b - a)
        n = np.array([-d[1], d[0]])
        if np.dot(center - (a + b) / 2, n) < 0:
            n = -n  # 内向きの法線
        cands = []
        for o1 in offs:
            pa = a + n * o1
            for o2 in offs:
                pb = b + n * o2
                pts = pa + (pb - pa) * t
                ins = np.clip(np.rint(pts + n * 4), 0, [w - 1, h - 1]).astype(int)
                out = np.clip(np.rint(pts - n * 4), 0, [w - 1, h - 1]).astype(int)
                vin = g[ins[:, 1], ins[:, 0]]
                diff = vin - g[out[:, 1], out[:, 0]]
                score = np.median(diff)
                # 内側が用紙の白さに届かない境目（画面の灰色の余白とタスクバーの境など）は選ばない
                if paper_check and np.median(vin) < 0.88 * paper:
                    score -= 1000
                cands.append((score, o1 + o2, pa, pb))
        c = max(cands, key=lambda c: (c[0], -abs(c[1])))
        lines.append((c[2], c[3]))
    corners = []
    for i in range(4):
        (p1, p2), (p3, p4) = lines[i - 1], lines[i]
        A = np.array([p2 - p1, p3 - p4]).T
        try:
            s_, _ = np.linalg.solve(A, p3 - p1)
        except np.linalg.LinAlgError:
            return q
        corners.append(p1 + (p2 - p1) * s_)
    out = _order(np.array(corners))
    # 合わせ直しで形が大きく崩れたら、元の四角形を使う
    if not cv2.isContourConvex(out.astype(np.int32)) or cv2.contourArea(out) < 0.5 * cv2.contourArea(q):
        return q
    return out


def find_page(bgr: np.ndarray) -> Optional[np.ndarray]:
    """いちばん大きい明るい四角形（用紙）を探す。見つからなければ None。"""
    h, w = bgr.shape[:2]
    scale = 1000 / max(h, w)
    small = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (7, 7), 0)
    otsu, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    hi = np.percentile(gray, 95) - 5
    # しきい値をいくつか試し、輪郭がいちばん素直な四角形になるものを選ぶ
    best = None
    for thr in np.linspace(otsu, max(otsu, hi), 8):
        c = _candidate(gray, thr)
        if c is None:
            continue
        quad, area, fit = c
        if not (0.2 <= area <= 0.97) or not (0.95 <= fit <= 1.02):
            continue
        key = (area,)
        if best is None or key > best[0]:
            best = (key, quad)
    if best is None:
        return None
    g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    q = best[1]
    q = _refine(g, q)
    return _order(q / scale)


def _size(q: np.ndarray) -> Tuple[int, int]:
    tl, tr, br, bl = q
    w = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2
    h = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2
    ratio = max(w, h) / min(w, h)
    if abs(ratio - A4) / A4 < 0.15:  # 募集図面はほぼA4・B4（同じ比率）なので、近ければその比率にそろえる
        ratio = A4
    long_, short = LONG_EDGE, round(LONG_EDGE / ratio)
    return (long_, short) if w >= h else (short, long_)


def clean(bgr: np.ndarray) -> np.ndarray:
    """照明のむら・画面の暗さを取り、紙の地を白く、文字をくっきりさせる。色や写真はそのまま残す。"""
    h, w = bgr.shape[:2]
    # モアレ（画面を撮ったときの細かい縞や虹色のにじみ）を和らげる。
    # 明るさはそのまま、色の成分だけをなめらかにするので、文字の形は変わらない
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    sig = max(2.0, min(bgr.shape[:2]) / 400)
    a = cv2.GaussianBlur(a, (0, 0), sig)
    b = cv2.GaussianBlur(b, (0, 0), sig)
    # 白い地に乗った薄い色（虹色のにじみ）は、無彩色に寄せる
    chroma = np.hypot(a.astype(np.float32) - 128, b.astype(np.float32) - 128)
    weak = np.clip((chroma - 6) / 10, 0, 1) * (l < 235) + (l >= 235) * np.clip((chroma - 12) / 10, 0, 1)
    a = (128 + (a.astype(np.float32) - 128) * weak).astype(np.uint8)
    b = (128 + (b.astype(np.float32) - 128) * weak).astype(np.uint8)
    img = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
    img = cv2.bilateralFilter(img, 5, 30, 5)
    # 紙の地の明るさを、写真や文字より大きな範囲の「明るいところ」から推定する（縮小して計算）
    f = 8
    small = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (w // f, h // f), interpolation=cv2.INTER_AREA)
    k = max(31, (min(small.shape) // 6) | 1)
    bg = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    bg = cv2.GaussianBlur(bg, (0, 0), k / 2)
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_CUBIC).astype(np.float32)
    gain = np.clip(250.0 / np.maximum(bg, 1), 1.0, 2.0)[..., None]
    out = np.clip(img.astype(np.float32) * gain, 0, 255).astype(np.uint8)
    # 地をさらに白へ、黒を少し締める（軽いレベル補正。写真がつぶれないよう控えめ）
    lut = np.clip((np.arange(256) - 12) * 255 / (235 - 12), 0, 255).astype(np.uint8)
    out = cv2.LUT(out, lut)
    # 輪郭を少しだけ強める
    blur = cv2.GaussianBlur(out, (0, 0), 1.0)
    return cv2.addWeighted(out, 1.3, blur, -0.3, 0)


def _trim_dark_edges(bgr: np.ndarray, limit: float = 0.03) -> np.ndarray:
    """用紙が写真の端まで写っているとき、外側に残ったモニターの枠などの暗い帯を切る（各辺最大3%）。"""
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = g.shape
    ref = np.percentile(g, 75)

    def count(lines):
        n = 0
        for v in lines:
            if v.mean() >= ref * 0.55:
                break
            n += 1
        return n
    top = min(count(g), int(h * limit))
    bottom = min(count(g[::-1]), int(h * limit))
    left = min(count(g.T), int(w * limit))
    right = min(count(g.T[::-1]), int(w * limit))
    return bgr[top:h - bottom, left:w - right]


def _looks_like_photo_of_page(bgr: np.ndarray, q: np.ndarray) -> bool:
    """見つけた四角形が「背景の上に置かれた用紙」らしいか。
    PDFの図面でも帯や色付きの縁で四角形が見つかることがあるため、
    向かい合う2辺の外側に背景が十分あり、背景が用紙より暗いときだけ写真とみなす。"""
    h, w = bgr.shape[:2]
    x0, y0 = q.min(0)
    x1, y1 = q.max(0)
    m = (x0 / w, 1 - x1 / w, y0 / h, 1 - y1 / h)
    if not ((m[0] > 0.04 and m[1] > 0.04) or (m[2] > 0.04 and m[3] > 0.04)):
        return False
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    inside = np.zeros((h, w), np.uint8)
    cv2.fillConvexPoly(inside, q.astype(np.int32), 1)
    return g[inside == 0].mean() < 0.75 * g[inside == 1].mean()


def scan(img: Image.Image) -> Tuple[Image.Image, str]:
    """写真を整えた画像と、どんな補正をしたか（台形補正あり／明るさ補正のみ／補正なし）を返す。
    PDFやスキャンした図面は、そのまま返す。"""
    bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    q = find_page(bgr)
    if q is not None and _looks_like_photo_of_page(bgr, q):
        w, h = _size(q)
        dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype="float32")
        bgr = cv2.warpPerspective(bgr, cv2.getPerspectiveTransform(q, dst), (w, h),
                                  flags=cv2.INTER_AREA, borderMode=cv2.BORDER_REPLICATE)
        bgr = _trim_dark_edges(bgr)
        status = "台形補正あり"
    elif is_camera(img):
        status = "明るさ補正のみ"
    else:
        return img, "補正なし"
    out = clean(bgr)
    return Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB)), status


def scan_with_corners(img: Image.Image, corners) -> Tuple[Image.Image, str]:
    """AIが答えた用紙の四隅（画像の幅・高さを1とした左上・右上・右下・左下）から整える。
    四隅はおおよその位置なので、近く（2.5%以内）の実際の用紙の縁に合わせてから台形補正する。"""
    bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    scale = 1000 / max(h, w)
    small = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    q = np.array([[x * small.shape[1], y * small.shape[0]] for x, y in corners], dtype="float32")
    q = _refine(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), q, radius=0.025, paper_check=False) / scale
    return _warp_and_clean(bgr, _order(q)), "台形補正あり（AIの四隅）"


def _warp_and_clean(bgr: np.ndarray, q: np.ndarray) -> Image.Image:
    w, h = _size(q)
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype="float32")
    bgr = cv2.warpPerspective(bgr, cv2.getPerspectiveTransform(q, dst), (w, h),
                              flags=cv2.INTER_AREA, borderMode=cv2.BORDER_REPLICATE)
    bgr = _trim_dark_edges(bgr)
    return Image.fromarray(cv2.cvtColor(clean(bgr), cv2.COLOR_BGR2RGB))


def is_camera(img: Image.Image) -> bool:
    """EXIFにカメラのメーカー・機種があるか（LINEで送られた写真では消えていることが多い）。"""
    if img.info.get("camera"):
        return True
    exif = img.getexif()
    return bool(exif.get(0x010F) or exif.get(0x0110))
