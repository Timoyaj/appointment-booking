# Tablekeeper — the product
#
# Builds a self-contained HTTP service: the reservation API, its published
# policies, recurring agreements, table closures and seating plans, the diner's
# four screens, and the restaurant's own console, on one port. Everything the
# runtime needs (Python, the web framework, the IANA timezone database, the
# application code, the stylesheets and the browser scripts) is baked in at build
# time; nothing is fetched when the container runs, so every screen renders with
# no outbound network.
#
# Two things differ from the reference image this grew out of, and both are
# about being safe to run for a real restaurant:
#
#   * TABLEKEEPER_TEST_HOOKS=0 — the unauthenticated /_test/* control endpoints
#     (reset, export, import) are not registered at all, so a stranger cannot
#     wipe the reservation book with one POST;
#   * TABLEKEEPER_DB=/data/tablekeeper.db — the database lives in a named volume
#     rather than /tmp, so a restart is not an erasure.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    TABLEKEEPER_DB=/data/tablekeeper.db \
    TABLEKEEPER_TEST_HOOKS=0

WORKDIR /app

# Dependencies first, so the layer is cached across code-only changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY tablekeeper/ ./tablekeeper/

# Run unprivileged. /data is where the database lives and where a volume should
# be mounted; owning it here means a named volume inherits that ownership.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app
USER appuser

# State outlives the container: mount this and a restart is not an erasure.
VOLUME ["/data"]

EXPOSE 8080

# Uses only the loopback interface, so it works with no outbound network.
HEALTHCHECK --interval=2s --timeout=3s --start-period=3s --retries=30 \
    CMD python -c "import os,sys,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/health', timeout=2)" || exit 1

CMD ["python", "-m", "tablekeeper.serve"]
