#!/usr/bin/env bash
# 报价变更采集离线演示：依次采集版本 1（建立基线）与版本 2（返回差异），再重复采集验证幂等。
# 前置：API 已在 8000 端口运行（或设置 API_URL）
#   uvicorn app.main:app --host 0.0.0.0 --port 8000
# 报价站点可由 Python 标准库静态托管，也可直接用 file://（脚本默认后者）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_URL="${API_URL:-http://127.0.0.1:8000/api/v1/quote-changes}"
SOURCE_KEY="${SOURCE_KEY:-demo-quote-site}"

call_api() {
  local url="$1"
  python3 - "$API_URL" "$SOURCE_KEY" "$url" <<'PY'
import json, os, sys, urllib.request

api, source_key, list_url = sys.argv[1:]
payload = {"source_key": source_key, "list_url": list_url}
request = urllib.request.Request(
    api,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=20) as resp:
    data = json.loads(resp.read())
print(data["status"], "snapshot=", data["snapshot_id"], "changes=", len(data["changes"]), "issues=", len(data["issues"]))
for change in data["changes"]:
    print("  ", change["plan_id"], change["change_type"], change["field"], change["old_value"], "->", change["new_value"])
for issue in data["issues"]:
    print("   issue:", issue["url"], issue["stage"], issue["reason"])
PY
}

echo "== 版本 1：首次完整采集，建立基线 =="
call_api "file://$ROOT/quote-site-v1/index.html"
echo "== 版本 2：与上次完整快照比较 =="
call_api "file://$ROOT/quote-site-v2/index.html"
echo "== 版本 2：重复采集，应无变更 =="
call_api "file://$ROOT/quote-site-v2/index.html"
