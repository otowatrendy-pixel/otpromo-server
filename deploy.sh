#!/bin/bash
# Cloud Run（東京）に載せる。Cloud Shell で実行: bash deploy.sh
set -euo pipefail
PROJECT=otpromo-maisoku
REGION=asia-northeast1
SERVICE=otpromo-api
BUCKET=otpromo-maisoku-files
SA=otpromo-api@${PROJECT}.iam.gserviceaccount.com
gcloud config set project $PROJECT

# 初回だけ：API・Firestore・保存場所・サービスアカウント
if [ "${1:-}" = "setup" ]; then
  gcloud services enable run.googleapis.com firestore.googleapis.com storage.googleapis.com \
    secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
  gcloud firestore databases create --location=$REGION --type=firestore-native 2>/dev/null || true
  gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access 2>/dev/null || true
  # 資料のファイルは30日で自動で消す
  echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' > /tmp/lc.json
  gcloud storage buckets update gs://$BUCKET --lifecycle-file=/tmp/lc.json
  gcloud iam service-accounts create otpromo-api --display-name="1分資料 API" 2>/dev/null || true
  gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$SA --role=roles/datastore.user --condition=None >/dev/null
  gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=serviceAccount:$SA --role=roles/storage.objectAdmin >/dev/null
  for s in MAISOKU_API_KEY LINE_CHANNEL_SECRET LINE_CHANNEL_ACCESS_TOKEN STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET APP_SECRET RESEND_API_KEY; do
    gcloud secrets create $s --replication-policy=automatic 2>/dev/null || true
    gcloud secrets add-iam-policy-binding $s --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor >/dev/null
  done
  echo "初回の準備が終わりました。秘密の値を登録してから bash deploy.sh を実行してください。"
  exit 0
fi

# 毎日9時（日本時間）に /cron/daily を呼ぶ：bash deploy.sh schedule
if [ "${1:-}" = "schedule" ]; then
  gcloud services enable cloudscheduler.googleapis.com
  URL=$(gcloud run services describe $SERVICE --region=$REGION --format='value(status.url)')
  KEY=$(gcloud secrets versions access latest --secret=APP_SECRET)
  # update は --headers を受け付けないため、あれば消して作り直す
  job() {
    gcloud scheduler jobs delete "$1" --location=$REGION --quiet >/dev/null 2>&1 || true
    gcloud scheduler jobs create http "$1" --location=$REGION --schedule="$2" --time-zone=Asia/Tokyo \
      --uri="$URL$3" --http-method=POST --headers="X-Cron-Key=$KEY" --attempt-deadline="$4" >/dev/null
  }
  job otpromo-daily "0 9 * * *" /cron/daily 300s
  # 5分ごと：受取りFAXの確認のお知らせ（10分後に1回）・預かった募集図面の削除・お支払いの猶予切れ・通数の警告
  job otpromo-often "*/5 * * * *" /cron/often 120s
  echo "毎日9時と5分ごと（日本時間）の定期実行を設定しました。"
  exit 0
fi

URL=$(gcloud run services describe $SERVICE --region=$REGION --format='value(status.url)' 2>/dev/null || true)
# 合言葉：ADMIN_USAGE_KEY（通数を返す /admin/usage）・ADMIN_OPS_KEY（一斉送信と「障害中」スイッチ）。なければ作ってしまっておく
for k in ADMIN_USAGE_KEY ADMIN_OPS_KEY; do
  if ! gcloud secrets versions list $k --limit=1 --format='value(name)' 2>/dev/null | grep -q .; then
    gcloud secrets create $k --replication-policy=automatic 2>/dev/null || true
    openssl rand -hex 24 | tr -d '\n' | gcloud secrets versions add $k --data-file=- >/dev/null
  fi
done
# あとから登録する秘密（運営専用LINEなど）も読めるように
for k in ADMIN_USAGE_KEY ADMIN_OPS_KEY ADMIN_LINE_TOKEN ADMIN_LINE_SECRET; do
  gcloud secrets add-iam-policy-binding $k --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor >/dev/null 2>&1 || true
done
SECRETS=""
# ADMIN_LINE_TOKEN・ADMIN_LINE_SECRET は運営専用LINE「1分資料 運営通知」（登録されていれば使う）
for s in MAISOKU_API_KEY LINE_CHANNEL_SECRET LINE_CHANNEL_ACCESS_TOKEN STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET APP_SECRET RESEND_API_KEY ADMIN_USAGE_KEY ADMIN_OPS_KEY ADMIN_LINE_TOKEN ADMIN_LINE_SECRET; do
  if gcloud secrets versions list $s --limit=1 --format='value(name)' 2>/dev/null | grep -q .; then
    SECRETS="${SECRETS}${SECRETS:+,}$s=$s:latest"
  fi
done
gcloud run deploy $SERVICE --source=. --region=$REGION --service-account=$SA \
  --allow-unauthenticated --no-cpu-throttling --cpu=2 --memory=2Gi --concurrency=8 --timeout=300 \
  --min-instances=0 --max-instances=3 \
  --set-env-vars="BASE_URL=${BASE_URL:-${URL:-https://example.invalid}},GCP_PROJECT=$PROJECT,BUCKET=$BUCKET,MODEL=claude-sonnet-5-5,EFFORT=low" \
  ${SECRETS:+--set-secrets=$SECRETS}
URL=$(gcloud run services describe $SERVICE --region=$REGION --format='value(status.url)')
# 初回は公開URLが決まってから BASE_URL を入れ直す
if [ -z "${BASE_URL:-}" ]; then
  gcloud run services update $SERVICE --region=$REGION --update-env-vars=BASE_URL=$URL >/dev/null
fi
echo "公開URL: $URL"
echo "LINE Webhook: $URL/line/webhook"
echo "Stripe Webhook: $URL/stripe/webhook"
