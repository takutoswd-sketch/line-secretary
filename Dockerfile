FROM python:3.11-slim

# ffmpeg インストール
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt --no-cache-dir

COPY . .

# Railway は $PORT を自動設定する
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}
