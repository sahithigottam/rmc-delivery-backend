FROM python:3.11-slim

WORKDIR /app

# Install minimal dependencies only
RUN pip install --no-cache-dir fastapi uvicorn[standard]

# Copy only standalone main.py and run script
COPY main_standalone.py main.py
COPY run.py run.py

EXPOSE 8000

# Use Python to properly handle environment variables
CMD ["python", "run.py"]
