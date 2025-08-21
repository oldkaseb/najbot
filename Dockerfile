# Railway deploys this service as a long-running worker
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1     PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps (optional; slim image already has most things we need)
RUN apt-get update && apt-get install -y --no-install-recommends     ca-certificates   && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Expose nothing (bot uses long polling). If you later move to webhooks, expose a port.
CMD ["python", "bot.py"]
