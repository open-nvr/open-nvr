# Use Python 3.11 slim image
FROM python:3.11-slim

WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies (needed for some python packages)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
RUN uv pip install --system --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Expose the port (Kai-C runs on 8100)
EXPOSE 8100

# Command to run the application
# We use python start.py as it launches uvicorn with the right config
CMD ["python", "start.py"]
