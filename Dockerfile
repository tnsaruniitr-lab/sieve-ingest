FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sieve_ingest ./sieve_ingest
# Default command runs one cycle; Railway cron invokes this on schedule.
CMD ["python", "-m", "sieve_ingest", "run"]
