FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY tunnel_qa ./tunnel_qa
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data/storage \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "tunnel_qa.main:app", "--host", "0.0.0.0", "--port", "8000"]

