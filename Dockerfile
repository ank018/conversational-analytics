# Conversational analytics - demo image.
#
# The warehouse ships inside the image. It is 52 MB, read-only, and identical
# to the file the evaluation ran against. Rebuilding it from Kaggle at startup
# would need credentials on the server and add minutes to every cold start,
# for no benefit.

FROM python:3.12-slim

# Fail fast and log immediately; a buffered container hides its own errors.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8501

WORKDIR /app

# Requirements first so the dependency layer survives code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY .streamlit/ ./.streamlit/
COPY src/ ./src/
COPY app/ ./app/
COPY docs/glossary.md ./docs/glossary.md
COPY data/olist.duckdb ./data/olist.duckdb

# The evaluation set travels with the image, so the deployed artefact carries
# the evidence for the accuracy it claims.
COPY eval/ ./eval/

# Run unprivileged. The app only reads, and the sandbox opens DuckDB
# read-only, but there is no reason for the process to be able to write.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8501

# `exec` replaces the shell with Streamlit so the process receives SIGTERM
# directly. Without it Docker signals /bin/sh, the app never hears about it,
# and the container is killed rather than stopped - which matters on Render,
# where instances stop and start routinely.
CMD ["sh", "-c", "exec streamlit run app/streamlit_app.py --server.port=${PORT} --server.address=0.0.0.0 --server.headless=true --browser.gatherUsageStats=false"]
