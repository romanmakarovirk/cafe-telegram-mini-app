#!/bin/bash
cd "/Users/romanmakarov/Documents/Шашлык и плов/New project"
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)
exec uvicorn main:app --host 0.0.0.0 --port 8000 --reload
