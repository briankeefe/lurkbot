FROM python:3.12-slim

WORKDIR /app

# Create data directory for SQLite persistence
RUN mkdir -p /app/data

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 5200

# Enable unbuffered output for logs
ENV PYTHONUNBUFFERED=1

# Health check for orchestration
HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5200/health')" || exit 1

# Run with uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5200"]