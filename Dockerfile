# Multi-stage build with explicit cache busting
FROM python:3.11-slim as dependencies

RUN pip install --no-cache-dir fastapi uvicorn[standard]

# Final stage - guaranteed clean
FROM python:3.11-slim

WORKDIR /app

# Copy dependencies from builder
COPY --from=dependencies /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=dependencies /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# Copy ONLY app files -.dockerignore ensures no old app/ directory
COPY main_standalone.py main.py
COPY run.py run.py

EXPOSE 8000

# Entrypoint ensures proper signal handling
ENTRYPOINT ["python", "run.py"]
