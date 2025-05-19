FROM python:3.13-alpine

WORKDIR /app

COPY app/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

EXPOSE 5000

ENV FLASK_ENV=production

# Run the NyaaSi RSS Downloader application
CMD ["python", "-m", "main"]