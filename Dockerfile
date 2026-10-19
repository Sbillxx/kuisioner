FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8765 \
    HEADLESS=true \
    DOCKER=1

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

# Copy project files
COPY . .

# Setup directory for temporary screenshots
RUN mkdir -p /app/hasil_cek && chmod -R 777 /app/hasil_cek

# Render assigns dynamic port via $PORT env var
EXPOSE 8765

# Run the web dashboard
CMD ["python", "simantap_web.py"]
