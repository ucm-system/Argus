#!/bin/sh
# Build once, then run the same image as the platform. All paths are host paths.
set -eu
: "${MODEL_DIR:?set MODEL_DIR to the local tokenizer directory}"
: "${DATA_DIR:?set DATA_DIR to the local dataset directory}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUTPUT_DIR=${OUTPUT_DIR:-"$SCRIPT_DIR/output/$(date +%Y%m%d-%H%M%S)-$$"}
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR=$(CDPATH= cd -- "$OUTPUT_DIR" && pwd)
MODEL_DIR=$(CDPATH= cd -- "$MODEL_DIR" && pwd)
DATA_DIR=$(CDPATH= cd -- "$DATA_DIR" && pwd)
exec docker run --rm --init --read-only --user "$(id -u):$(id -g)" \
  --tmpfs /tmp:rw,exec,size=4g --shm-size=1g \
  -v "$OUTPUT_DIR:/workspace/output" \
  -v "$MODEL_DIR:/models/model:ro" -v "$DATA_DIR:/data:ro" \
  -v "$DATA_DIR:/benchmark/ais_bench/datasets:ro" \
  -e ENDPOINT_API_KEY -e METRICS_API_KEY \
  "${TESTER_IMAGE:-ghcr.io/ucm-system/argus-runner-aisbench-perf:0.3.5}" \
  --model_path=/models/model "$@"
