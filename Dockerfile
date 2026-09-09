FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

EXPOSE 8787
# ONE worker, deliberately. The Meta+Branch result cache is in-process; a second worker
# would keep its own copy and double the API calls while halving the hit rate. Threads
# carry the concurrency instead.
#
# --timeout 900, raised from 300 on 2026-09-09. The hourly refresh builds every brand and
# window in one request: measured 214s for today+yesterday and 555s once 7d is included.
# At 300s Cloud Run cut the request and Cloud Scheduler recorded DEADLINE_EXCEEDED every
# hour, so the refresh never finished and the page went stale while the job looked like
# it was running. 900 matches the scheduler's own attempt deadline, so all three limits --
# scheduler, Cloud Run request, gunicorn worker -- now agree.
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT:-8787} server:app --workers 1 --threads 8 --timeout 900 --graceful-timeout 30"]
