FROM python:3.11-slim

# Security: run as non-root
RUN groupadd -r botuser && useradd -r -g botuser botuser

WORKDIR /app

# Install dependencies first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create logs directory with correct permissions
RUN mkdir -p logs && chown -R botuser:botuser /app

USER botuser

# Expose dashboard + metrics ports
EXPOSE 5000 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/metrics', timeout=5)" || exit 1

CMD ["python", "main.py"]
