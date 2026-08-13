#!/bin/bash
#SBATCH --job-name=snowmass-postprocess
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=00:30:00
#SBATCH --output=logs/%x-%A_%a.out
#SBATCH --error=logs/%x-%A_%a.err

set -euo pipefail
: "${SNOWMASS_SNAPSHOT_ROOT:?SNOWMASS_SNAPSHOT_ROOT is required}"
: "${SNOWMASS_PYTHON:?SNOWMASS_PYTHON is required}"

mkdir -p "$SNOWMASS_SNAPSHOT_ROOT/logs"
cd "$SNOWMASS_SNAPSHOT_ROOT/repo"
exec "$SNOWMASS_PYTHON" -u scripts/snowmass_hpc_postprocess.py \
  --snapshot-root "$SNOWMASS_SNAPSHOT_ROOT/data" \
  --task-manifest "$SNOWMASS_SNAPSHOT_ROOT/data/tasks.json"
