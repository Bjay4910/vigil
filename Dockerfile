FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY store/ ./store/
COPY detection/ ./detection/

RUN useradd --create-home --shell /bin/sh vigil \
    && mkdir -p /data \
    && chown -R vigil:vigil /data /app

VOLUME /data

ENV PYTHONUNBUFFERED=1
ENV VIGIL_DATA_PATH=/data/points.jsonl

USER vigil

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys, urllib.request; resp = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4); sys.exit(0 if resp.getcode() == 200 else 1)"

CMD ["python", "-m", "uvicorn", "store.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

