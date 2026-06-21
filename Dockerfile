FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY templates ./templates

# Data dir is bind-mounted from the host; created here so it exists even
# if the mount is absent (e.g. local testing).
RUN mkdir -p /app/data

EXPOSE 5007

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5007"]
