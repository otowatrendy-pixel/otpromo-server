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
TAX_RATE = env("STRIPE_TAX_RATE")  # 消費税10%（外税）の txr_...。空なら起動時に探すか作る

# フォームのリンクに付ける署名の鍵
APP_SECRET = env("APP_SECRET", "dev-only-secret")

# Firebase（アプリ版のログイン確認）
FIREBASE_PROJECT = env("FIREBASE_PROJECT", "otpromo-maisoku")

# 料金と枚数
FREE_SHEETS = int(env("FREE_SHEETS", "5"))       # 最初の無料枚数（通算）
LIGHT_SHEETS = int(env("LIGHT_SHEETS", "50"))    # ライトの月の枚数（繰り越しなし）
FILE_DAYS = int(env("FILE_DAYS", "7"))           # PDFのリンクの有効日数
