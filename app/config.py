"""設定はすべて環境変数から読む（秘密の値は Cloud Run に Secret Manager から渡す）。"""
import os


def env(name, default=""):
    return os.environ.get(name, default)


# 公開URL（Webhook・登録フォーム・PDFのリンクに使う）。末尾の / なし
BASE_URL = env("BASE_URL", "http://localhost:8080").rstrip("/")

# 保存先。STORE=local ならメモリとローカルフォルダだけで動く（手元の確認用）
STORE = env("STORE", "gcp")
GCP_PROJECT = env("GCP_PROJECT", "otpromo-maisoku")
BUCKET = env("BUCKET", "otpromo-maisoku-files")
LOCAL_DIR = env("LOCAL_DIR", "/tmp/otpromo-local")

# Claude
ANTHROPIC_KEY = env("MAISOKU_API_KEY") or env("ANTHROPIC_API_KEY")
MODEL = env("MODEL", "claude-sonnet-5-5")
EFFORT = env("EFFORT", "low")
MOCK_RESULT = env("MOCK_RESULT")  # JSONファイルの場所。入っていればAIを呼ばない（動作確認用）

# LINE（Messaging API）
LINE_CHANNEL_SECRET = env("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = env("LINE_CHANNEL_ACCESS_TOKEN")
LINE_ADD_FRIEND_URL = env("LINE_ADD_FRIEND_URL", "https://lin.ee/tlnsKca")

# Stripe（まずはテスト環境のキー）
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET")
PRICE_LIGHT = env("PRICE_LIGHT", "price_1UUsZOB542nWQrMKc0HZD2WL")
PRICE_UNLIMITED = env("PRICE_UNLIMITED", "price_1UVHfFB542nWQrMKwZKbd3uZ")
# 年払い（税込。件数・回数は月払いと同じく毎月戻す。途中解約は返金なし・期間の終わりまで使える）
PRICE_LIGHT_YEAR = env("PRICE_LIGHT_YEAR", "price_1UVJygB542nWQrMKfPqpDrzx")          # 年額14,800円
PRICE_UNLIMITED_YEAR = env("PRICE_UNLIMITED_YEAR", "price_1UVJyhB542nWQrMKZRxg4Gsd")  # 年額29,800円
# 店舗まとめ割（1人あたり月額・税込）。受付はサービス公開日（STORE_CAMPAIGN_START）から3か月
PRICE_GROUP_LIGHT = env("PRICE_GROUP_LIGHT", "price_1UVKSxB542nWQrMKvDBQepAF")          # 1,180円
PRICE_GROUP_UNLIMITED = env("PRICE_GROUP_UNLIMITED", "price_1UVKSxB542nWQrMK8EVLk1FV")  # 2,480円
INVOICE_TRN = env("INVOICE_TRN", "T5011601011495")  # 適格請求書発行事業者の登録番号（T＋13桁）。入れると領収書・請求書に載る
TERMS_URL = env("TERMS_URL", "https://otpromo.com/terms-ippun.html")
TERMS_VERSION = env("TERMS_VERSION", "2026-10-28")  # 規約を改めたら日付を変える（同意の記録と照らす）
STORE_CAMPAIGN_START = env("STORE_CAMPAIGN_START", "2026-11-11")  # サービス開始日（10/28本人決定）。空なら受付中
# 新人さん応援：期間中に初めて月払いを申し込んだ方は最初の月が半額（1回だけ50%引きのクーポン）
NEWBIE_START = env("NEWBIE_START", "2027-03-01")
NEWBIE_END = env("NEWBIE_END", "2027-04-30")
NEWBIE_COUPON = env("NEWBIE_COUPON", "newbie_half")
TAX_RATE = env("STRIPE_TAX_RATE")  # 消費税10%（外税）の txr_...。空なら起動時に探すか作る

# フォームのリンクに付ける署名の鍵
APP_SECRET = env("APP_SECRET", "dev-only-secret")

# Firebase（アプリ版のログイン確認）
FIREBASE_PROJECT = env("FIREBASE_PROJECT", "otpromo-maisoku")

# 料金と枚数
FREE_SHEETS = int(env("FREE_SHEETS", "5"))       # 最初の無料枚数（通算）
LIGHT_SHEETS = int(env("LIGHT_SHEETS", "50"))    # ライトの月の枚数（繰り越しなし）
FILE_DAYS = int(env("FILE_DAYS", "7"))           # PDFのリンクの有効日数
