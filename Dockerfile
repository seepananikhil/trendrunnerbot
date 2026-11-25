FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY broker_kotak.py market_data.py option_pricing.py strategy_engine.py risk_guardian.py telegram_notifier.py telegram_bot.py main.py portfolio_config.json ./

RUN mkdir -p /app/data/live /app/data/kotak /app/data/performance /app/logs

CMD ["python", "-u", "main.py"]
