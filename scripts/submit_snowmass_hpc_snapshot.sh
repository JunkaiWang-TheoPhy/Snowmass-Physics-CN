#!/bin/bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 SSH_ALIAS LOCAL_SNAPSHOT REMOTE_ROOT" >&2
  exit 2
fi
alias_name=$1
snapshot=$2
remote_root=$3
job_name=snowmass-postprocess
[[ -f "$snapshot/tasks.json" ]] || { echo "missing tasks.json" >&2; exit 2; }
task_count=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["task_count"])' "$snapshot/tasks.json")
[[ "$task_count" =~ ^[1-9][0-9]*$ ]] || { echo "invalid non-positive task count" >&2; exit 2; }

active=$(/usr/bin/ssh -T "$alias_name" "squeue -h -u \"\$USER\" -n '$job_name' -o '%i %T'" | tail -n 20)
[[ -z "$active" ]] || { echo "refusing duplicate active job: $active" >&2; exit 3; }
/usr/bin/ssh -T "$alias_name" "mkdir -p '$remote_root/data' '$remote_root/repo/scripts' '$remote_root/repo/translations' '$remote_root/repo/site/assets' '$remote_root/logs'"
/usr/bin/rsync -a --delete "$snapshot/" "$alias_name:$remote_root/data/"
/usr/bin/rsync -a scripts/ "$alias_name:$remote_root/repo/scripts/"
/usr/bin/rsync -a translations/ "$alias_name:$remote_root/repo/translations/"
/usr/bin/rsync -a site/assets/ "$alias_name:$remote_root/repo/site/assets/"
/usr/bin/ssh -T "$alias_name" "cd '$remote_root' && sbatch --array=1-$task_count --export=ALL,SNOWMASS_SNAPSHOT_ROOT='$remote_root',SNOWMASS_PYTHON=python3 repo/scripts/slurm/snowmass-postprocess-array.sbatch"
