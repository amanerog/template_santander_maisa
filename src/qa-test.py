"""
QA Test Script - Golden dataset validation for Maisa agents.

Pre-check mode  (no TARGET_MAISA_URL / MAISA_AUTH_CREDENTIAL): prints "OK" and exits.
Post-import mode (credentials provided): finds the worker by name, runs each test case
from scripts/qa-dataset.json against it, and prints "OK" if all pass or "KO" + details
if any fail.

Required env vars (post-import mode):
  TARGET_MAISA_URL        Base Maisa URL (e.g. https://gppaas-maisa-plat.sgtech.pre.corp)
  MAISA_AUTH_CREDENTIAL   Full cookie string or bearer token
  ORGANIZATION_ID         Maisa organization ID
  WORKSPACE_ID            Maisa workspace ID

Optional env vars:
  AUTH_TYPE               "cookie" (default) or "api_key"
  TEST_WM_ID              Worker Manager ID to test directly — skips the name search.
                          Use this when testing an already-deployed agent without importing.

qa-dataset.json format:
{
  "agent_name": "My Worker",
  "tests": [
    {
      "id": "test-1",
      "description": "Basic arithmetic",
      "input_variables": { "query": "What is 2+2?" },
      "expected": "4",
      "match": "contains"
    }
  ]
}

match values: "contains" (default), "exact", "not_empty"
input_variables: dict of variable name → value sent to the worker.
"""

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).parent
TIMEOUT = 120
POLL_INTERVAL = 3
POLL_MAX_ATTEMPTS = 40  # up to ~2 min


@dataclass
class MaisaAPI:
    base_url: str
    auth_credential: str
    auth_type: str = "cookie"

    def _headers(self) -> Dict[str, str]:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; MaisaQATest/1.0)"}
        if self.auth_type == "api_key":
            headers["Authorization"] = f"Bearer {self.auth_credential}"
        else:
            headers["Cookie"] = self.auth_credential
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/maisa-bff{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        content_type: Optional[str] = None,
        body: Optional[bytes] = None,
    ) -> Any:
        import httpx

        url = self._url(path)
        while True:
            with httpx.Client(timeout=TIMEOUT, verify=False, follow_redirects=True) as client:
                headers = self._headers()
                if content_type:
                    headers["Content-Type"] = content_type
                elif json_body is not None:
                    headers["Content-Type"] = "application/json"

                kwargs: Dict[str, Any] = {"headers": headers}
                if json_body is not None:
                    kwargs["json"] = json_body
                if params is not None:
                    kwargs["params"] = params
                if body is not None:
                    kwargs["content"] = body

                resp = client.request(method, url, **kwargs)

                if resp.status_code == 429:
                    print("  ⏳ Rate limited, waiting 2s...")
                    time.sleep(2)
                    continue

                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

                if not resp.content:
                    return None
                return resp.json()

    def get(self, path: str, **kw: Any) -> Any:
        return self._request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self._request("POST", path, **kw)

    def get_paginated(self, path: str, *, page_size: int = 100) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page = 1
        while True:
            resp = self.get(path, params={"limit": page_size, "page": page})
            if isinstance(resp, dict):
                data = resp.get("data", [])
                if isinstance(data, dict):
                    page_items = data.get("items", data.get("workerManagers", []))
                elif isinstance(data, list):
                    page_items = data
                else:
                    page_items = []
            else:
                page_items = []

            if not page_items:
                break
            items.extend(page_items)

            pagination = resp.get("pagination", {}) if isinstance(resp, dict) else {}
            if not pagination.get("hasNextPage", False):
                break
            page += 1
        return items


def _build_multipart(fields: Dict[str, str]) -> Tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        parts.append(f"{v}\r\n".encode())
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def load_dataset() -> dict:
    dataset_path = SCRIPT_DIR / "qa-dataset.json"
    if not dataset_path.exists():
        raise SystemExit(f"❌ Golden dataset not found: {dataset_path}")
    with open(dataset_path, encoding="utf-8") as f:
        return json.load(f)


def get_worker_manager_by_id(api: MaisaAPI, wm_id: str) -> Dict[str, Any]:
    resp = api.get(f"/worker-manager/{wm_id}")
    data = resp.get("data", resp) if isinstance(resp, dict) else resp
    if not isinstance(data, dict) or not data.get("id"):
        raise SystemExit(f"❌ Worker manager '{wm_id}' not found or returned unexpected response.")
    return data


def find_worker_manager(api: MaisaAPI, name: str, org_id: str, workspace_id: str) -> Dict[str, Any]:
    wm_list = api.get_paginated(f"/organizations/{org_id}/workspaces/{workspace_id}/worker-managers")
    name_lower = name.lower()
    match = next((w for w in wm_list if (w.get("name") or "").lower() == name_lower), None)
    if not match:
        available = [w.get("name") for w in wm_list]
        raise SystemExit(f"❌ Worker '{name}' not found in workspace. Available: {available}")
    return match


def run_worker(api: MaisaAPI, wm_id: str, version_id: str, input_variables: Dict[str, str]) -> str:
    ivars = [{"name": k, "value": v} for k, v in input_variables.items()]
    body, ct = _build_multipart({"inputVariables": json.dumps(ivars)})

    resp = None
    for path in [f"/workers/{version_id}/run", f"/digital-worker/{wm_id}/run"]:
        try:
            resp = api.post(path, body=body, content_type=ct)
            break
        except RuntimeError as e:
            print(f"    ⚠️  {path} failed: {e}")

    if resp is None:
        raise RuntimeError("All run endpoints failed")

    data = resp.get("data", resp) if isinstance(resp, dict) else resp
    exec_id = ""
    if isinstance(data, dict):
        exec_id = data.get("executionId") or data.get("id") or ""
    if not exec_id:
        raise RuntimeError(f"No execution ID in run response: {str(resp)[:200]}")

    print(f"    ⏱️  Execution ID: {exec_id} — polling for result...")
    for _ in range(POLL_MAX_ATTEMPTS):
        time.sleep(POLL_INTERVAL)
        detail = api.get(f"/executions/{exec_id}")
        d = detail.get("data", detail) if isinstance(detail, dict) else detail
        status = (d.get("status") or "").lower() if isinstance(d, dict) else ""
        if status in ("completed", "failed", "error"):
            result = (d.get("result") or "") if isinstance(d, dict) else ""
            return str(result)

    raise RuntimeError(f"Execution {exec_id} did not finish after {POLL_MAX_ATTEMPTS * POLL_INTERVAL}s")


def check_response(content: str, expected: str, match: str) -> bool:
    if match == "not_empty":
        return bool(content.strip())
    if match == "exact":
        return content.strip().lower() == expected.strip().lower()
    return expected.lower() in content.lower()


def main() -> None:
    target_url = os.environ.get("TARGET_MAISA_URL", "").strip()
    auth_credential = os.environ.get("MAISA_AUTH_CREDENTIAL", "").strip()

    if not target_url or not auth_credential:
        print("OK")
        return

    auth_type = os.environ.get("AUTH_TYPE", "cookie").strip()
    org_id = os.environ.get("ORGANIZATION_ID", "").strip()
    workspace_id = os.environ.get("WORKSPACE_ID", "").strip()

    if not org_id or not workspace_id:
        raise SystemExit("❌ ORGANIZATION_ID and WORKSPACE_ID env vars are required in post-import mode.")

    dataset = load_dataset()
    tests: List[dict] = dataset.get("tests", [])
    if not tests:
        raise SystemExit("❌ qa-dataset.json has no test cases")

    agent_name = dataset.get("agent_name", "").strip()

    api = MaisaAPI(base_url=target_url, auth_credential=auth_credential, auth_type=auth_type)

    test_wm_id = os.environ.get("TEST_WM_ID", "").strip()

    if test_wm_id:
        print(f"🔍 Using provided Worker Manager ID: {test_wm_id}")
        wm = get_worker_manager_by_id(api, test_wm_id)
        agent_name = wm.get("name", test_wm_id)
    else:
        if not agent_name:
            raise SystemExit("❌ 'agent_name' is required in qa-dataset.json when TEST_WM_ID is not set.")
        if not org_id or not workspace_id:
            raise SystemExit("❌ ORGANIZATION_ID and WORKSPACE_ID are required when TEST_WM_ID is not set.")
        print(f"🔍 Finding worker by name: {agent_name}")
        wm = find_worker_manager(api, agent_name, org_id, workspace_id)

    wm_id = wm.get("id", "")
    version_id = wm.get("lastVersionId") or wm.get("deployedVersionId") or ""

    if not version_id:
        raise SystemExit(f"❌ Worker '{agent_name}' has no lastVersionId or deployedVersionId")

    print(f"🤖 Testing worker: {agent_name} (wm_id={wm_id}, version={version_id})")
    print(f"📋 {len(tests)} test case(s) to run")
    print("=" * 60)

    results: List[bool] = []
    for test in tests:
        test_id = test.get("id", "?")
        description = test.get("description", "")
        label = f"[{test_id}] {description}".strip()

        # Support both "input_variables" (dict) and "input" (plain string → sent as "query")
        input_variables = test.get("input_variables")
        if input_variables is None:
            plain_input = test.get("input", "")
            input_variables = {"query": plain_input}

        print(f"\n  {label}")
        print(f"    Input    : {json.dumps(input_variables)[:120]}")

        try:
            content = run_worker(api, wm_id, version_id, input_variables)
            passed = check_response(content, test.get("expected", ""), test.get("match", "contains"))
            print(f"    Response : {content[:120]}")
            print(f"    Expected : {test.get('expected', '')} (match={test.get('match', 'contains')})")
            print(f"    Result   : {'✅ PASS' if passed else '❌ FAIL'}")
        except Exception as e:
            print(f"    ❌ Error running test: {e}")
            passed = False

        results.append(passed)

    passed_count = sum(results)
    total = len(results)

    print("\n" + "=" * 60)
    print(f"Results: {passed_count}/{total} passed")

    if all(results):
        print("OK")
    else:
        print("KO")
        sys.exit(1)


main()
