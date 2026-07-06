FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY app ./app

RUN mkdir -p /data/input /data/output /data/cache /data/jobs /data/logs

EXPOSE 7860

CMD ["python", "app.py"]
