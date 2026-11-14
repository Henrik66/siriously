#!/usr/bin/env bash
# Download Bonsai 2 4B (Q2_0 GGUF) + a stock llama.cpp build (>= b11556) into <repo>/models (gitignored).
# Idempotent; verifies the model's SHA-256. Usage: scripts/bonsai/setup.sh [cpu|vulkan]   (default: cpu)
# macOS: the macos-arm64 build uses Metal regardless of the argument (or just `brew install llama.cpp`).
set -euo pipefail

KIND="${1:-cpu}"
BUILD="${BONSAI_LLAMA_BUILD:-b12383}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
MODELS="${BONSAI_MODELS_DIR:-$REPO/models}"
MODEL_FILE="Ternary-Bonsai-2-4B-Q2_0.gguf"
MODEL_SHA256="64a5183b44921bdb5ac4a6f5f92007821d6ebe5c66997d3c147fd214ab9433d4"
MODEL_URL="https://huggingface.co/prism-ml/Ternary-Bonsai-2-4B-gguf/resolve/main/$MODEL_FILE"

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
fetch() { echo "  GET $1"; curl -L --fail --retry 3 -C - -o "$2.part" "$1" && mv "$2.part" "$2"; }

[ "${BUILD#b}" -ge 11556 ] || { echo "Q2_0 needs llama.cpp >= b11556 (got $BUILD)" >&2; exit 1; }

mkdir -p "$MODELS/bonsai"
dest="$MODELS/bonsai/$MODEL_FILE"
if [ ! -f "$dest" ] || [ "$(sha256 "$dest")" != "$MODEL_SHA256" ]; then
  fetch "$MODEL_URL" "$dest"
  [ "$(sha256 "$dest")" = "$MODEL_SHA256" ] || { rm -f "$dest"; echo "SHA-256 mismatch for $MODEL_FILE" >&2; exit 1; }
fi
echo "model ok: $dest"

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) asset="llama-$BUILD-bin-macos-arm64.tar.gz"; KIND="metal" ;;
  Linux-x86_64) [ "$KIND" = vulkan ] && asset="llama-$BUILD-bin-ubuntu-vulkan-x64.tar.gz" || asset="llama-$BUILD-bin-ubuntu-x64.tar.gz" ;;
  Linux-aarch64) asset="llama-$BUILD-bin-ubuntu-arm64.tar.gz" ;;
  *) echo "no prebuilt llama.cpp for $(uname -s)-$(uname -m); build it or use brew" >&2; exit 1 ;;
esac
bin="$MODELS/llama.cpp/$BUILD/$KIND"
if [ ! -x "$bin/llama-server" ]; then
  tmp="$(mktemp -d)"
  fetch "https://github.com/ggml-org/llama.cpp/releases/download/$BUILD/$asset" "$tmp/$asset"
  mkdir -p "$bin"
  tar -xzf "$tmp/$asset" -C "$tmp"
  srv="$(find "$tmp" -name llama-server -type f | head -n1)"
  [ -n "$srv" ] || { echo "llama-server not found in $asset" >&2; exit 1; }
  cp -R "$(dirname "$srv")/." "$bin/"
  rm -rf "$tmp"
fi
echo "llama.cpp $BUILD ($KIND) ok: $bin/llama-server"
echo "setup done. Start with: scripts/bonsai/start.sh $KIND"
