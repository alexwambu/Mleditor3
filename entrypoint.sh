#!/bin/bash
set -e
echo "[entrypoint] starting FastAPI server..."
uvicorn main:app --host 0.0.0.0 --port 8000
