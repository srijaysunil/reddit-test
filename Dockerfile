FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py
COPY templates /app/templates

# Runtime data directory (DB + uploads)
ENV UPLOAD_DIR=/data/uploads \
    APP_TIMEZONE=UTC \
    FLASK_SECRET_KEY=dev-secret-change-me \
    MAX_UPLOAD_MB=10

RUN mkdir -p /data/uploads && chmod -R 777 /data

EXPOSE 5000

CMD ["python", "app.py"]
