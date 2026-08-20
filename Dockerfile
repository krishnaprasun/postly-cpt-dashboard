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
# carry the concurrency instead. --timeout 180 because a 7-day pull takes ~30s locally
# and longer on a free instance's shared CPU; gunicorn's 30s default would kill it.
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT:-8787} server:app --workers 1 --threads 8 --timeout 180 --graceful-timeout 30"]
