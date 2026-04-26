FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY analytics/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY analytics/ .
CMD ["sleep", "infinity"]
