FROM python:3.9-slim

WORKDIR /app

# Install system dependencies if any (none for core strictly, maybe curl for healthcheck)
# RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Expose Port
EXPOSE 8081

# Command
CMD ["python3", "main.py"]
