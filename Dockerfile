# Multi-stage build to eliminate Docker cache issues
FROM python:3.11-slim as builder

RUN pip install --no-cache-dir fastapi uvicorn[standard]

# Final stage - completely separate, forces fresh build
FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# Copy ONLY new files - no old code
COPY main_standalone.py main.py
COPY run.py run.py

EXPOSE 8000

CMD ["python", "run.py"]
