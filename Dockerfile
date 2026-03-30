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

CMD ["python", "main.py"]
