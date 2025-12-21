FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pillow support implies dependencies if image generation was needed, but here qrcode needs pure python usually or basic libs.
# If Pillow is needed by qrcode, it might need compile tools, but qrcode standard is fine.

COPY . .

# Expose port (8081 is what we used in main.py)
EXPOSE 8081

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081"]
