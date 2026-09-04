FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY . .

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py", "run"]
