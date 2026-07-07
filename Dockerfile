FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 POLL_INTERVAL=1800 INFLUX_PORT=8086 INFLUX_DB=vehicle MEASUREMENT=kia REGION=3 BRAND=1 FORCE_REFRESH=false TOKEN_FILE=/data/kia_token.pkl NTFY_URL=https://ntfy.sh NTFY_TOPIC= NTFY_ON_START=false
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kia_influx.py .
VOLUME ["/data"]
CMD ["python", "kia_influx.py"]
