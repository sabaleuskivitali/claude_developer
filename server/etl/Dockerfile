FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    && rm -rf /var/lib/apt/lists/*

COPY etl/requirements.txt /app/etl/requirements.txt
RUN pip install --no-cache-dir -r /app/etl/requirements.txt

COPY etl/ /app/etl/

COPY etl/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
