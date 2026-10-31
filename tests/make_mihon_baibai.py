"""売買の見本図面（架空）を描く。python tests/make_mihon_baibai.py → tests/fixtures/mihon-baibai.png
物件名・会社名・地名はすべて架空（メゾン見本・見本エステート・○○区見本町）。"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
F = "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"
W, H = 1654, 2339  # A4 200dpi
im = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(im)
f = lambda s: ImageFont.truetype(F, s)
d.text((80, 70), "中古マンション　メゾン見本 302号室", font=f(64), fill="black")
d.text((80, 170), "価格 3,980万円", font=f(90), fill=(200, 30, 30))
d.rectangle((80, 300, 900, 1050), outline="black", width=3); d.text((330, 640), "（間取り図）", font=f(48), fill="gray")
rows = [("所在地", "東京都○○区見本町2丁目"), ("交通", "見本線 見本駅 徒歩6分"), ("専有面積", "65.12㎡（壁芯）"),
        ("間取り", "3LDK"), ("築年月", "1980年4月"), ("構造", "RC造 8階建の3階"), ("管理費", "12,300円/月"),
        ("修繕積立金", "15,800円/月"), ("土地権利", "所有権"), ("現況", "居住中"), ("引渡し", "相談"),
        ("取引態様", "専任媒介"), ("備考", "告知事項あり（詳細はお問合せ）")]
y = 1100
for k, v in rows:
    d.rectangle((80, y, 1574, y + 62), outline="black", width=2); d.line((380, y, 380, y + 62), fill="black", width=2)
    d.text((100, y + 14), k, font=f(34), fill="black"); d.text((400, y + 14), v, font=f(34), fill="black"); y += 62
d.text((80, y + 30), "手数料：分かれ　広告転載不可　レインズ番号 100123456789", font=f(34), fill="black")
d.text((80, y + 150), "売主：見本ホーム株式会社　TEL 03-0000-3333", font=f(34), fill="black")
d.text((80, y + 90), "【内見】前日までに要予約　TEL 03-0000-1111", font=f(34), fill="black")
d.rectangle((0, 2120, W, H), fill=(30, 60, 140))
d.text((60, 2150), "見本エステート株式会社", font=f(60), fill="white")
d.text((60, 2240), "東京都知事(2)第00000号　TEL 03-0000-1111　FAX 03-0000-2222", font=f(36), fill="white")
out = Path(__file__).parent / "fixtures" / "mihon-baibai.png"; im.save(out); print(out)
