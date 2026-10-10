# Build context is the repository root (see docker-compose.yml).
FROM python:3.12.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app

COPY dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY intelligence/ intelligence/
COPY dashboard/app.py dashboard/app.py

RUN useradd --uid 10001 --no-create-home appuser
ENV HOME=/tmp
USER 10001

EXPOSE 8501
CMD ["streamlit", "run", "dashboard/app.py", "--server.address=0.0.0.0", "--server.port=8501", \
     "--server.headless=true", "--browser.gatherUsageStats=false", "--client.toolbarMode=minimal"]
