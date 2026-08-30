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
# carry the concurrency instead. --timeout 300 because a 30-day ad-set fold measures
# ~116s on a developer laptop and a free instance's shared CPU is slower still; at the
# old 180s the worker was killed mid-fold, which reads as the page hanging and then
# failing. gunicorn's 30s default would kill almost everything here.
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT:-8787} server:app --workers 1 --threads 8 --timeout 300 --graceful-timeout 30"]
