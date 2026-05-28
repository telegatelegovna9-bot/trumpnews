FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install --with-deps chromium

# Copy app
COPY . .

# Create screenshots directory
RUN mkdir -p screenshots

CMD ["python", "-u", "main.py"]
