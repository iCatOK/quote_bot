FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Нужен git для установки pinterest_downloader из GitHub
# libjpeg/zlib — для Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    libgomp1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY . .

# Создаём каталоги для кэша заранее
RUN mkdir -p /app/bg_cache /app/emoji_cache /app/fonts

CMD ["python", "main.py"]