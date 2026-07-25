FROM python:3.11-slim

# Install Tesseract + Chromium system deps
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    libtesseract-dev \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download Chromium browser binaries only
RUN playwright install chromium

COPY . .

ENV PORT=10000
EXPOSE 10000

# Workers=1 prevents Playwright threading issues on low-RAM plans
CMD gunicorn --bind 0.0.0.0:$PORT --timeout 180 --workers 1 --threads 2 app:app