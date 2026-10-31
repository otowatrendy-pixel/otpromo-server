"""売買の見本図面（架空）を描く。python tests/make_mihon_baibai.py → tests/fixtures/mihon-baibai.png
物件名・会社名・地名はすべて架空（メゾン見本・見本エステート・○○区見本町）。"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
F = "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"
W, H = 1654, 2339  # A4 200dpi
im = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(im)
f = lambda s: ImageFont.truetype(F, s)
d.text((80, 60), "中古マンション　メゾン見本 302号室", font=f(60), fill="black")
d.text((80, 150), "価格 3,980万円", font=f(86), fill=(200, 30, 30))
d.text((900, 175), "南西向き・角部屋・2022年リフォーム済", font=f(36), fill=(30, 60, 140))
# 間取り図（左）と、設備・周辺環境（右）
d.rectangle((80, 270, 860, 860), outline="black", width=3); d.text((320, 540), "（間取り図）", font=f(44), fill="gray")
box = [("■ リフォーム（2022年6月）", 30), ("キッチン・浴室・洗面台・トイレ交換", 28), ("全室クロス・床張替え", 28),
       ("■ 設備", 30), ("追い焚き・浴室乾燥機・食洗機", 28), ("システムキッチン・オートロック", 28), ("宅配ボックス・ウォークインクローゼット", 28),
       ("■ 周辺環境", 30), ("スーパー見本マート 徒歩3分（約240m）", 28), ("見本小学校 徒歩5分（約400m）", 28),
       ("見本中央公園 徒歩2分（約160m）", 28), ("見本総合病院 徒歩8分（約640m）", 28)]
d.rectangle((890, 270, 1574, 860), outline="black", width=2)
y = 290
for t, s in box:
    d.text((910, y), t, font=f(s), fill="black"); y += 46 if s == 30 else 42
rows = [("所在地", "東京都○○区見本町2丁目"), ("交通", "見本線 見本駅 徒歩6分／見本東線 見本東駅 徒歩11分"),
        ("専有面積", "65.12㎡（壁芯）"), ("バルコニー", "8.40㎡（南西向き）"), ("間取り", "3LDK"),
        ("築年月", "1980年4月"), ("構造・階", "RC造 8階建の3階（角部屋）"), ("総戸数", "32戸"),
        ("管理", "全部委託・日勤（見本管理株式会社）"), ("管理費", "12,300円/月"), ("修繕積立金", "15,800円/月"),
        ("駐車場", "空きあり 月額15,000円（要確認）"), ("ペット", "小型犬・猫1匹まで可（管理規約による）"),
        ("土地権利", "所有権"), ("用途地域", "第一種住居地域"), ("現況", "居住中"), ("引渡し", "相談"),
        ("取引態様", "専任媒介"), ("備考", "告知事項あり（詳細はお問合せ）")]
y = 890
for k, v in rows:
    d.rectangle((80, y, 1574, y + 50), outline="black", width=2); d.line((330, y, 330, y + 50), fill="black", width=2)
    d.text((96, y + 11), k, font=f(28), fill="black"); d.text((350, y + 11), v, font=f(28), fill="black"); y += 50
d.text((80, y + 20), "手数料：分かれ　広告転載不可　レインズ番号 100123456789", font=f(30), fill="black")
d.text((80, y + 70), "【内見】前日までに要予約　TEL 03-0000-1111", font=f(30), fill="black")
d.text((80, y + 120), "売主：見本ホーム株式会社　TEL 03-0000-3333", font=f(30), fill="black")
d.rectangle((0, 2140, W, H), fill=(30, 60, 140))
d.text((60, 2160), "見本エステート株式会社", font=f(58), fill="white")
d.text((60, 2250), "東京都知事(2)第00000号　TEL 03-0000-1111　FAX 03-0000-2222", font=f(36), fill="white")
out = Path(__file__).parent / "fixtures" / "mihon-baibai.png"; im.save(out); print(out)
