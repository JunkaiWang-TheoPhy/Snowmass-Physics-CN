#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_root="${SNOWMASS_PDF2ZH_RUNTIME:-/Users/Zhuanz/.local/share/snowmass-tools/pdf2zh-next-2.9.0}"
lock_file="${repo_root}/requirements/snowmass-pdf2zh-next.lock"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to recreate the isolated pdf2zh-next runtime" >&2
  exit 1
fi
if [[ ! -f "${lock_file}" ]]; then
  echo "missing dependency lock: ${lock_file}" >&2
  exit 1
fi

if [[ ! -x "${runtime_root}/bin/python" ]]; then
  uv venv "${runtime_root}" --python 3.12
fi

env \
  -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY \
  -u http_proxy -u https_proxy -u all_proxy -u no_proxy \
  uv pip sync --python "${runtime_root}/bin/python" "${lock_file}"

env \
  -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY \
  -u http_proxy -u https_proxy -u all_proxy -u no_proxy \
  "${runtime_root}/bin/python" - <<'PY'
import importlib.metadata

expected = {"pdf2zh-next": "2.9.0", "babeldoc": "0.6.4"}
for package, version in expected.items():
    installed = importlib.metadata.version(package)
    if installed != version:
        raise SystemExit(f"{package}={installed}; expected {version}")
print("isolated pdf2zh-next runtime verified")
PY
