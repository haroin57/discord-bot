FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    KNOWLEDGE_HOST=0.0.0.0 KNOWLEDGE_DB_PATH=/data/invest_kb PORT=8095 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 RAYON_NUM_THREADS=2
WORKDIR /app
COPY deploy/offload/knowledge-requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home knowledge
COPY services/__init__.py /app/services/__init__.py
COPY services/offload /app/services/offload
COPY services/invest_kb /app/services/invest_kb
USER 10001
EXPOSE 8095
CMD ["python", "-m", "services.offload.knowledge_server"]
