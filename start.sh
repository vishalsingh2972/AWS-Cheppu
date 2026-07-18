#!/usr/bin/env bash
# VoiceOps AI – Backend startup script
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

echo "╔══════════════════════════════════╗"
echo "║        VoiceOps AI Backend       ║"
echo "╚══════════════════════════════════╝"

# Load .env if present
if [ -f "$BACKEND_DIR/.env" ]; then
  echo "► Loading environment from .env"
  export $(grep -v '^#' "$BACKEND_DIR/.env" | xargs)
else
  echo "⚠  No .env found. Copy backend/.env.example to backend/.env and configure."
fi

# Check AWS credentials
if ! aws sts get-caller-identity --query Account --output text &>/dev/null; then
  echo "⚠  AWS credentials not configured. Run: aws configure"
fi

# Install dependencies
echo "► Installing Python dependencies..."
pip install -r "$BACKEND_DIR/requirements.txt" -q

# Start server
echo "► Starting server on http://localhost:8000"
echo ""
cd "$BACKEND_DIR"
uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}" --reload --log-level "${LOG_LEVEL:-info}"
