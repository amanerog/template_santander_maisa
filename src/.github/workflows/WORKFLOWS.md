# Maisa GitHub Actions Workflows

This document describes all GitHub Actions workflows implemented for managing Maisa Digital Workers across environments.

---

## Secret Configuration

All Maisa workflows share a single GitHub secret:

**Secret name:** `MAISA_AUTH_CREDENTIAL`

**Format:**
```json
{
  "dev": {
    "url": "https://gppaas-maisa.dev.sgtech.corp",
    "cookie": "maisaai-session-id=xxx; AWSALBTG=yyy; AWSALBTGCORS=yyy"
  },
  "pre": {
    "url": "https://gppaas-maisa-plat.sgtech.pre.corp",
    "cookie": "maisaai-session-id=aaa; AWSALBTG=bbb; AWSALBTGCORS=bbb"
  },
  "pro": {
    "url": "https://gppaas-maisa.sgtech.pro.corp",
    "cookie": "maisaai-session-id=ccc; AWSALBTG=ddd; AWSALBTGCORS=ddd"
  }
}
```

Each environment key maps to the base URL and the full browser cookie string for that environment. The `cookie` value is copied from DevTools → Network → any Maisa request → Request Headers → Cookie.

> **Note:** `AWSALBTG` / `AWSALBTGCORS` cookies expire in approximately 1 week. When pipelines start failing with 401, refresh the cookie values in the secret.

---

## Branch Structure

| Branch | Purpose |
|---|---|
| default branch (`main`) | Source of the release tags and of the QA scripts (`scripts/qa-test.py`, `scripts/qa-dataset.json`), which are read from the tagged commit |
| `development` | Used by `test-agents.yml` to read the QA scripts |
| `feature/export-data` | Storage for `export-agents.yml` / `export-config-agents.yml` output. Not part of the promotion chain |

---

## Workflows

### `export-agents.yml` — Export a single agent

Exports one Maisa Digital Worker from a source environment and saves it as a `.mai` file in the `feature/export-data` branch.

**Trigger:** Manual (`workflow_dispatch`) or called from another workflow.

**Inputs:**

| Input | Required | Description |
|---|---|---|
| `worker_id` | Yes | Worker version ID to export (`lastVersionId` from the worker manager) |
| `workspace_name` | Yes | Git folder name for storage: `agents/<workspace_name>/` |
| `environment` | Yes | Key in `MAISA_AUTH_CREDENTIAL` (e.g. `dev`, `pre`, `pro`) |

**What it does:**
1. Checks out `feature/export-data`
2. Resolves the URL and cookie from `MAISA_AUTH_CREDENTIAL[environment]`
3. Calls `GET /maisa-bff/workers/{worker_id}/export`
4. Saves the response as `agents/<workspace_name>/<YYYYMMDD-HHMMSS>/{worker_id}.mai`
5. Commits and pushes to `feature/export-data`

**Output file location:** `agents/<workspace_name>/<timestamp>/<worker_id>.mai`

---

### `export-config-agents.yml` — Export agent configurations

Exports the configuration (metadata JSON) of all worker managers in a workspace. Useful for inspecting worker settings without the full binary export.

**Trigger:** Manual (`workflow_dispatch`) or called from another workflow.

**Inputs:**

| Input | Required | Description |
|---|---|---|
| `workspace_name` | Yes | Git folder name for storage |
| `organization_id` | Yes | Maisa organization ID |
| `workspace_id` | Yes | Maisa workspace ID |
| `environment` | Yes | Key in `MAISA_AUTH_CREDENTIAL` |
| `mode` | No | `all` (default) or `single` |
| `worker_manager_id` | No | Worker Manager ID (only if `mode=single`) |

**What it does:**
1. Lists all worker managers via `GET /maisa-bff/organizations/{org}/workspaces/{ws}/worker-managers`
2. Fetches full details for each via `GET /maisa-bff/worker-manager/{id}`
3. Saves each as `{name}__{status}__{id}.json` under `agents/<workspace_name>/<timestamp>/`
4. Commits and pushes to `feature/export-data`

---

### `release-agent.yml` — Publish an agent as a release

Exports one agent from a source environment and publishes it as a GitHub Release. This is the only way an agent can enter the promotion chain.

**Trigger:** Manual (`workflow_dispatch`) or called from another workflow. Only runs from the default branch.

**Inputs:** `worker_id` (version ID to export), `agent_name` (lowercase, digits and dashes), `environment` (source key in `MAISA_AUTH_CREDENTIAL`).

**Result:**
- Tag `agent-<agent_name>-<YYYYMMDD-HHMMSS>` on the current default-branch commit (automatic versioning)
- Assets `<agent_name>.mai` and `<agent_name>.mai.sha256`

**Manual fallback:** if the bot cannot create tags/releases, a user can create the release by hand with the same tag format and attach the `.mai` and its `.sha256` (`sha256sum x.mai > x.mai.sha256`). The tag must be on the default branch history.

---

### `import-agents.yml` — Deploy a release into Maisa

Imports the `.mai` of a release into a target environment. The release tag is the only accepted source.

**Trigger:** Manual (`workflow_dispatch`) or called from another workflow.

**Inputs:**

| Input | Required | Description |
|---|---|---|
| `release_tag` | Yes | `agent-<name>-<YYYYMMDD-HHMMSS>` |
| `target_organization_id` | Yes | Target Maisa organization ID |
| `target_workspace_id` | Yes | Target Maisa workspace ID |
| `environment` | Yes | `dev`, `pre` or `pro` (key in `MAISA_AUTH_CREDENTIAL`) |
| `import_mode` | No | `new_worker` (default) or `new_version` |
| `target_wm_id` | No | Worker Manager ID to update (only if `import_mode=new_version`) |

**Import modes:**

| Mode | API call | When to use |
|---|---|---|
| `new_worker` | `POST /maisa-bff/organizations/{org}/workspaces/{ws}/workers/import` | Creating the agent for the first time in the target |
| `new_version` | `POST /maisa-bff/workers/{target_wm_id}/import?mode=newVersion` | Updating an existing agent with a new version |

**Jobs:**

```
validate-release → qa-test (pre-check) → deploy  [GitHub Environment approval]
```

1. **validate-release:** the tag has the expected format, the release is published with exactly one `.mai` and one `.sha256`, the tag belongs to the default-branch history, and for `pro` the release already has `deployed-pre.txt`.
2. **qa-test:** runs `scripts/qa-test.py` from the tagged commit without credentials — must print `OK`.
3. **deploy:** waits for approval on the GitHub Environment, downloads the asset, verifies the sha256, imports it, runs the QA dataset and `scripts/post_import.py` (if present) from the tagged commit, and uploads `deployed-<env>.txt` to the release.

**Environment mapping and approvals:**

| `environment` | GitHub Environment | Approval |
|---|---|---|
| `dev` | `certification` | None |
| `pre` | `preproduction` | None (optional reviewers) |
| `pro` | `production` | **Required reviewers (human in the loop)** |

Configure them in *Settings → Environments*. For `production`: add required reviewers, enable *Prevent self-review* and restrict deployment branches to the default branch.

> **Note:** approval only protects what runs under the environment. While `MAISA_AUTH_CREDENTIAL` is a single repository secret containing the `pro` cookie, anyone able to run workflows can read it. Move the `pro` credential to an environment secret on `production` to make the gate effective.

---

### `test-agents.yml` — Test a deployed agent

Runs the QA test suite against an already-deployed agent without importing anything. Use this to validate an existing agent or re-run tests after a manual change.

**Trigger:** Manual (`workflow_dispatch`) or called from another workflow.

**Inputs:**

| Input | Required | Description |
|---|---|---|
| `test_wm_id` | Yes | Worker Manager ID of the agent to test |
| `environment` | Yes | Key in `MAISA_AUTH_CREDENTIAL` |
| `target_organization_id` | No | Maisa organization ID (only needed when finding agent by name) |
| `target_workspace_id` | No | Maisa workspace ID (only needed when finding agent by name) |

**What it does:**
1. Checks out `development` branch to read `scripts/qa-test.py` and `scripts/qa-dataset.json`
2. Calls `GET /maisa-bff/worker-manager/{test_wm_id}` to get `lastVersionId`
3. Runs each test case: `POST /maisa-bff/workers/{lastVersionId}/run` → polls `GET /maisa-bff/executions/{id}` until `completed`
4. Prints `OK` if all pass, `KO` if any fail

---

### `promote-agent.yml` — Release and deploy to pre

Chains `release-agent` (from the source environment) and `import-agents` (to `pre`) in one run.

**Trigger:** Manual (`workflow_dispatch`) only.

**Inputs:** `worker_id`, `agent_name`, `source_environment`, `target_organization_id`, `target_workspace_id`, `import_mode`, `target_wm_id`.

**Promoting to pro:** run **Import Maisa Agents** with the same `release_tag` and `environment=pro`. The same artifact tested in pre is deployed, after a reviewer approves the `production` environment. Nothing is re-exported.

A `concurrency` group on `agent_name` prevents parallel promotions of the same agent.

---

### `cd.yml` — Deploy

Standard Gluon deployment workflow. Delegates to the shared `gln-workflows` reusable workflow. It deploys to AWS, so it is **not part of the Maisa promotion flow** (`release-agent` → `import-agents`).

**Inputs:** `version`, `environment`, `environment-type` (`certification` / `preproduction` / `production`), `task-number`

---

### `create-release-branch.yml` — Create release branch

Creates a release branch from a tag. Delegates to the shared `gln-workflows` reusable workflow.

**Inputs:** `tag`, `version`, `gluon-runner`

---

### `update-component-workflow.yml` — Update CI/CD workflows

Updates the reusable CI/CD workflows from the shared `gln-workflows` repository.

---

## QA Test Script

**File:** `scripts/qa-test.py`  
**Dataset:** `scripts/qa-dataset.json`

Both files live in the `development` branch.

### Pre-check mode
When `MAISA_AUTH_CREDENTIAL` or `MAISA_ENVIRONMENT` are not set, the script immediately prints `OK` and exits. This is used as a gate before import to verify the script exists and is syntactically valid.

### Post-import mode
When credentials are present, the script:
1. Resolves URL and cookie from `MAISA_AUTH_CREDENTIAL[MAISA_ENVIRONMENT]`
2. If `TEST_WM_ID` is set: fetches the worker manager directly by ID
3. Otherwise: searches by `agent_name` from `qa-dataset.json` in the workspace
4. Gets `lastVersionId` from the worker manager
5. For each test case: runs the worker and polls for the result
6. Prints `OK` if all pass, `KO` + details if any fail

### `qa-dataset.json` format

```json
{
  "agent_name": "My Worker Name",
  "tests": [
    {
      "id": "test-1",
      "description": "Basic sanity check",
      "input_variables": { "query": "Hello" },
      "expected": "",
      "match": "not_empty"
    },
    {
      "id": "test-2",
      "description": "Keyword check",
      "input_variables": { "query": "What is 2 plus 2?" },
      "expected": "4",
      "match": "contains"
    }
  ]
}
```

**Match types:**

| Value | Behaviour |
|---|---|
| `not_empty` | Response must not be empty |
| `contains` | Response must contain `expected` (case-insensitive) |
| `exact` | Response must equal `expected` (case-insensitive) |
