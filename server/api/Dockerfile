FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy API source and bootstrap module (shared library)
COPY api/ .
COPY bootstrap/ ./bootstrap/

ARG APP_VERSION=dev
RUN echo "$APP_VERSION" > /app/VERSION
RUN chmod +x entrypoint.sh && mkdir -p /app/runtime /app/certs

ENTRYPOINT ["./entrypoint.sh"]
