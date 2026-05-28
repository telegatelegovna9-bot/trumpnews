FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install both Chromium and Firefox (Firefox is less detectable by Cloudflare)
RUN playwright install --with-deps chromium firefox

# Copy app
COPY . .

RUN mkdir -p screenshots

CMD ["python", "-u", "main.py"]
