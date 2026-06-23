# わたなべ部長クローン — Cloud Run 用 Dockerfile
#
# 使い方:
#   gcloud run deploy boss-clone-web --source . --region us-central1 ...
#
# 環境変数（Cloud Run 側で --set-env-vars で渡す）:
#   GOOGLE_CLOUD_PROJECT
#   GOOGLE_CLOUD_LOCATION=us-central1
#   GOOGLE_GENAI_USE_VERTEXAI=TRUE
# Day 4 後半（L-009）以降、Vector Search は使わず Firestore + アプリ内 cos 類似度。
# VS_PAIR_* 環境変数は不要。

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# システム依存（最小限）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python 依存（layer cache を効かせるため requirements.txt のみ先にコピー）
COPY requirements.txt ./
RUN pip install -r requirements.txt

# アプリ本体
COPY scripts/ ./scripts/
COPY assets/ ./assets/

# Cloud Run は PORT 環境変数を渡してくる（デフォルト 8080）
ENV PORT=8080
EXPOSE 8080

# Streamlit を Cloud Run 用設定で起動
# exec form で書くと PORT 変数が展開されないので shell form を使用
CMD streamlit run scripts/boss_clone_web.py \
    --server.port=${PORT} \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
