FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

WORKDIR /app

# Install Xvfb system packages (needed by pyvirtualdisplay)
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    xauth \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install browsers
RUN playwright install --with-deps chromium firefox

COPY . .

RUN mkdir -p screenshots

CMD ["python", "-u", "main.py"]
