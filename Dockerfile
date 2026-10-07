FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY snaplist ./snaplist
COPY scripts ./scripts
# Models are baked into the image (checksums verified) so containers start fast.
RUN python scripts/download_models.py

RUN useradd --create-home --uid 10001 snaplist && mkdir -p /data && chown snaplist /data
USER snaplist
ENV SNAPLIST_STORAGE_DIR=/data SNAPLIST_MODEL_DIR=/app/models

EXPOSE 8300
CMD ["uvicorn", "snaplist.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8300"]
