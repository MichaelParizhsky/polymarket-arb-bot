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
# PORT=8080 is set explicitly in Railway env vars.
EXPOSE 8080

CMD ["python", "main.py"]
