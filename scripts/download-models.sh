#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_DIR="$(dirname "$SCRIPT_DIR")/models"
mkdir -p "$MODELS_DIR"

download_if_missing() {
  local filename="$1"
  local url="$2"
  local target="$MODELS_DIR/$filename"

  if [[ -f "$target" ]]; then
    echo "[skip] $filename already exists"
    return 0
  fi

  echo "[download] $filename"
  curl -fL --progress-bar -o "${target}.part" "$url"
  mv "${target}.part" "$target"
  echo "[done] $filename"
}

download_if_missing \
  "Llama-3.2-1B-Instruct-Q4_K_M.gguf" \
  "https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf"

download_if_missing \
  "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf" \
  "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"

download_if_missing \
  "all-MiniLM-L6-v2-ggml-model-f16.gguf" \
  "https://huggingface.co/second-state/All-MiniLM-L6-v2-Embedding-GGUF/resolve/main/all-MiniLM-L6-v2-ggml-model-f16.gguf"

