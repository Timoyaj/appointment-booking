# Tablekeeper — Stage 2
#
# Builds a self-contained HTTP service: the reservation API and the browser
# product on one port. Everything the runtime needs (Python, the web framework,
# the IANA timezone database, the application code, the stylesheet and the
# browser script) is baked in at build time; nothing is fetched when the
# container runs, so the screens render with no outbound network.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    TABLEKEEPER_DB=/tmp/tablekeeper/tablekeeper.db

WORKDIR /app

# Dependencies first, so the layer is cached across code-only changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY tablekeeper/ ./tablekeeper/

# Run unprivileged; the database lives in /tmp, which the container owns.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /tmp/tablekeeper \
    && chown -R appuser:appuser /tmp/tablekeeper /app
USER appuser

EXPOSE 8080

# Uses only the loopback interface, so it works with no outbound network.
HEALTHCHECK --interval=2s --timeout=3s --start-period=3s --retries=30 \
    CMD python -c "import os,sys,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/health', timeout=2)" || exit 1

CMD ["python", "-m", "tablekeeper.serve"]
