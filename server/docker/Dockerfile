FROM python:3.14-slim

# Set working directory
WORKDIR /app

# Install system dependencies for pyserial
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories for logs
RUN mkdir -p /app/logs

# Expose Flask port
EXPOSE 5000

# Run the application
CMD ["python", "run.py"]
