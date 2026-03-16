FROM python:3.11-slim

# System deps for numpy/sklearn
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create log dir
RUN mkdir -p logs

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import json, pathlib; \
        s = json.loads(pathlib.Path('logs/stats.json').read_text()); \
        print('OK', s.get('total_trades', 0), 'trades')" || exit 1

CMD ["python", "-u", "bot.py"]
