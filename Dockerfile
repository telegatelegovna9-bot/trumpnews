FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Firefox only — less detectable by Cloudflare
RUN playwright install --with-deps firefox

COPY . .

RUN mkdir -p screenshots

CMD ["python", "-u", "main.py"]
