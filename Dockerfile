FROM python:3.11-slim

WORKDIR /app

# tesseract-ocr backs pytesseract for scanned documents/images (menus,
# invoices, quotations); the assistant degrades to a plain "no text found"
# result without it rather than failing to build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY . .

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py", "run"]
