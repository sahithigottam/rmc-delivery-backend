FROM python:3.11-slim

WORKDIR /app

# Install minimal dependencies only
RUN pip install --no-cache-dir fastapi uvicorn[standard]

# Copy only standalone main.py - not the app directory
COPY main_standalone.py main.py

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
