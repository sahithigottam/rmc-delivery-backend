FROM python:3.11-slim

WORKDIR /app

# Install minimal dependencies only
RUN pip install --no-cache-dir fastapi uvicorn[standard]

# Copy only standalone main.py - not the app directory
COPY main_standalone.py main.py

EXPOSE 8000

CMD sh -c "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"
