#!/bin/bash

CONDA_BASE="/Users/hasnatchowdhury/opt/anaconda3"

APP_DIR="/Users/hasnatchowdhury/Programming/homeScroller"
ROOT_DIR="/Users/hasnatchowdhury/Desktop/New Videos/newer_videos/all_instagram"

cd "$APP_DIR" || exit 1

# Run inside the neuro4ml conda environment (no activation needed)
exec "$CONDA_BASE/bin/conda" run -n neuro4ml \
  python app.py \
  --root "$ROOT_DIR" \
  --host 0.0.0.0 \
  --port 5179
