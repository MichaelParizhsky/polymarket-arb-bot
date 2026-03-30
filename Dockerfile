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

# Health check — port 5000 matches EXPOSE above (Railway sets PORT=5000 from EXPOSE)
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:5000/', timeout=5)" || exit 1

CMD ["python", "main.py"]
