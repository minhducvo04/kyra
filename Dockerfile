# Kyra API (v2 slice 1). Web layer only: the audio stack and local MLX models are
# Apple-Silicon/laptop concerns and are not installed here.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    KYRA_HOST=0.0.0.0 KYRA_PORT=8420 KYRA_DATA_DIR=/data
WORKDIR /app
# LaTeX for the one-page resume loop (pdflatex only; xelatex users add texlive-xetex).
RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-latex-base texlive-latex-recommended texlive-fonts-recommended texlive-latex-extra \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-web.txt ./
RUN pip install -r requirements-web.txt
COPY src ./src
COPY web ./web
COPY scripts/web_ui.py scripts/worker.py ./scripts/
VOLUME ["/data"]
EXPOSE 8420
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8420/api/backend',timeout=3)" || exit 1
CMD ["python", "scripts/web_ui.py", "--host", "0.0.0.0", "--port", "8420"]
