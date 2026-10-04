"""Minimal FastAPI application for Railway deployment"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Minimal app - no complex imports
app = FastAPI(title="RMC Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "RMC Backend"}

@app.get("/")
async def root():
    return {"message": "RMC Backend API"}
