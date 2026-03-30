FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create logs directory
RUN mkdir -p logs

# Expose dashboard port. Railway routes $PORT here; metrics stay on 8000 (internal only).
EXPOSE 5000

# Health check on the dashboard (respects $PORT, falls back to 5000)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import httpx,os; httpx.get(f'http://localhost:{os.getenv(\"PORT\",\"5000\")}/', timeout=5)" || exit 1

CMD ["python", "main.py"]
