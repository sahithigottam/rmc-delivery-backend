#!/usr/bin/env python
"""Start uvicorn with proper PORT handling for Railway"""
import os
import subprocess
import sys

port = os.getenv("PORT", "8000")
print(f"Starting FastAPI on port {port}...")

# Run uvicorn with the port
subprocess.run([
    "uvicorn",
    "main:app",
    "--host", "0.0.0.0",
    "--port", str(port)
], check=True)
