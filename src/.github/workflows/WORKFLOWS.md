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

---

## Workflows

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
validate-release → qa-test (pre-check) → approval (pro only) → deploy  [GitHub Environment gate]
```

1. **validate-release:** the tag has the expected format, the release is published with exactly one `.mai` and one `.sha256`, the tag belongs to the default-branch history, and for `pro` the release already has `deployed-pre.txt`.
2. **qa-test:** runs `scripts/qa-test.py` from the tagged commit without credentials — must print `OK`.
3. **approval (pro only):** waits up to 4 hours for a human decision (see `approve-deployment.yml`). Fails if it is rejected or times out.
4. **deploy:** waits for the GitHub Environment gate (if configured), downloads the asset, verifies the sha256, imports it, runs the QA dataset and `scripts/post_import.py` (if present) from the tagged commit, and uploads `deployed-<env>.txt` to the release.

**Environment mapping and approvals:**

| `environment` | GitHub Environment | Approval |
|---|---|---|
| `dev` | `certification` | None |
| `pre` | `preproduction` | None (optional reviewers) |
| `pro` | `production` | **Required reviewers (human in the loop)** |

Configure them in *Settings → Environments*. For `production`: add required reviewers, enable *Prevent self-review* and restrict deployment branches to the default branch.

> **Note:** the `approval` job is a provisional gate that does not need repository settings. If an administrator configures required reviewers on the `production` environment, the two gates stack; the `approval` job can then be removed. Approval also does not protect the `pro` credential while `MAISA_AUTH_CREDENTIAL` is a single repository secret containing the `pro` cookie: move it to an environment secret on `production` once that environment can be configured.

---

### `approve-deployment.yml` — Approve a deployment to pro

Registers the decision of an approver for a deployment to pro that is waiting in `import-agents.yml`.

**Trigger:** Manual (`workflow_dispatch`) only.

**Inputs:** `deploy_run_id` (number at the end of the URL of the waiting run, also printed in its summary) and `decision` (`approve` / `reject`).

**Rules:**
- Only logins listed in `.github/pro-approvers.txt` count. The file lives under `.github/`, so only its code owners can change it.
- The person who launched the deployment can never approve it.
- The waiting job re-checks the approver from the list of runs of this workflow, so a run by an unlisted user has no effect.
- Approvals made before the deployment job started are ignored; re-running the deployment requires a new approval.
- A `reject` from any valid approver fails the deployment.

**Flow:**
1. Launch *Import Maisa Agents* with `environment=pro`. The `approval` job prints the run ID and the link to this workflow.
2. Tell an approver. They run *Approve Maisa Deployment* with that run ID and `approve`.
3. The `approval` job detects it within 30 seconds and the deployment continues.

The `approval` job occupies a runner while it waits (maximum 4 hours).

---

### `test-agents.yml` — Test a deployed agent

Runs the QA test suite against an already-deployed agent without importing anything. Use this to validate an existing agent or re-run tests after a manual change.

**Trigger:** Manual (`workflow_dispatch`) or called from another workflow.

**Inputs:**

| Input | Required | Description |
|---|---|---|
| `release_tag` | Yes (manual) | Release tag (`agent-<name>-<YYYYMMDD-HHMMSS>`) whose scripts and dataset are used. When called from `promote-agent` it is empty and the scripts of the current default-branch commit are used |
| `test_wm_id` | Yes | Worker Manager ID of the agent to test |
| `environment` | Yes | Key in `MAISA_AUTH_CREDENTIAL` |
| `target_organization_id` | No | Maisa organization ID (only needed when finding agent by name) |
| `target_workspace_id` | No | Maisa workspace ID (only needed when finding agent by name) |

**What it does:**
1. Checks out the release tag to read `scripts/qa-test.py` and `scripts/qa-dataset.json`
2. Calls `GET /maisa-bff/worker-manager/{test_wm_id}` to get `lastVersionId`
3. Runs each test case: `POST /maisa-bff/workers/{lastVersionId}/run` → polls `GET /maisa-bff/executions/{id}` until `completed`
4. Prints `OK` if all pass, `KO` if any fail

---

### `promote-agent.yml` — Test, release and deploy to pre

Chains `test-agents` (QA in the source environment), `release-agent` and `import-agents` (to `pre`) in one run. The release is only created if the QA tests pass in the source environment.

```
test-source → release → import (pre)
```

**Trigger:** Manual (`workflow_dispatch`) only.

**Inputs:** `worker_id`, `source_wm_id` (Worker Manager ID in the source environment, used by the QA tests), `agent_name`, `source_environment`, `target_organization_id`, `target_workspace_id`, `import_mode`, `target_wm_id`.

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

Both files are read from the tagged commit of the release (`import-agents.yml` and `test-agents.yml`).

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
