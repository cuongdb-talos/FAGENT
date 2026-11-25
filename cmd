#!/bin/bash
rm -r output
set -e
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export OLLAMA_API_KEY="${OLLAMA_API_KEY:-ollama}"
exec python3 -m FAGENT --setup env_setup.yaml  --verbose "$@"
