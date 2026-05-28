FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

WORKDIR /app

# Install Xvfb for virtual display
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    xauth \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install --with-deps chromium

# Copy app
COPY . .

RUN mkdir -p screenshots

# xvfb-run automatically starts virtual display and sets $DISPLAY
CMD ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1920x1080x24", "python", "-u", "main.py"]
