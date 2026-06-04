"""
MCP Server – MAISA Studio
Protocolo: JSON-RPC 2.0 sobre stdio (Model Context Protocol).
Sin dependencias externas (solo stdlib).

Herramientas expuestas:
  - maisa_env              → Muestra entorno actual y disponibles
  - maisa_switch_env       → Cambia de entorno (47 entornos: argentina_dev, chile_pro, uk_pre, etc.)
  - maisa_list_workspaces  → Lista workspaces del entorno activo
  - maisa_list_workers     → Lista workers de un workspace
  - maisa_get_worker       → Detalle de un worker (por nombre o ID)
  - maisa_get_executions   → Lista ejecuciones con metricas Phase 1 (T_h, TF)
  - maisa_get_execution    → Detalle de una ejecucion individual con tokens
  - maisa_worker_profiler  → Profiler heuristico de un worker (categoria, complejidad, riesgos)
  - maisa_create_worker    → Crea un Digital Worker
  - maisa_deploy_worker    → Despliega (publica) un worker
  - maisa_run_worker       → Ejecuta un worker desplegado
  - maisa_add_member       → Agrega usuario como ws_manager a un workspace

Configuracion Windsurf:  Anadir a MCP settings:
  {
    "mcpServers": {
      "maisa": {
        "command": "python",
        "args": ["maisa_mcp_server.py"],
        "cwd": "<ruta_al_proyecto>"
      }
    }
  }
"""

import json
import os
import ssl
import sys
import uuid
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Logging to stderr (stdout is reserved for MCP protocol) ──────────────────

def log(msg: str):
    sys.stderr.write(f"[MCP] {msg}\n")
    sys.stderr.flush()

# ── SSL ──────────────────────────────────────────────────────────────────────

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ── Directory of this script ─────────────────────────────────────────────────

_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Environments (imported from maisa_web.py - single source of truth) ───────

try:
    sys.path.insert(0, _DIR)
    from maisa_web import ENVIRONMENTS as _WEB_ENVS
    from maisa_web import (
        CATEGORY_RATES, COMPLEXITY_MULTIPLIERS, H_GLOBAL,
        _run_heuristic_profiler, _run_llm_profiler, _calc_phase1,
    )
    ENVIRONMENTS = {}
    for eid, ev in _WEB_ENVS.items():
        ENVIRONMENTS[eid] = dict(ev)
        # Ensure cookie_file is absolute
        cf = ev.get("cookie_file", "")
        if cf and not os.path.isabs(cf):
            ENVIRONMENTS[eid]["cookie_file"] = os.path.join(_DIR, cf)
    log(f"Loaded {len(ENVIRONMENTS)} environments + Phase 1 engine from maisa_web.py")
except Exception as _e:
    log(f"Failed to import from maisa_web.py: {_e}, using fallback")
    ENVIRONMENTS = {
        "dev_global": {
            "label": "DEV Global",
            "api_base": "https://gppaas-maisa.dev.sgtech.corp/maisa-bff",
            "cookie_file": os.path.join(_DIR, ".maisa_cookie"),
        },
    }

_current_env = "dev_global"

# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _load_cookie(env_id: str) -> str:
    path = ENVIRONMENTS[env_id]["cookie_file"]
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def _api(env_id: str, method: str, path: str, body: bytes = None,
         content_type: str = "application/json", timeout: int = 30) -> dict:
    """HTTP request to the Maisa BFF/Studio API."""
    env = ENVIRONMENTS[env_id]
    url = f"{env['api_base']}{path}"
    stored = _load_cookie(env_id)
    if not stored:
        return {"_error": True, "reason": "No hay cookie/token de sesion. Abre la UI (localhost:9090) y autenticate."}
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", content_type)
    if env.get("auth_type") == "bearer":
        req.add_header("Authorization", f"Bearer {stored}")
    else:
        req.add_header("Cookie", stored)
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8")
        return json.loads(resp_body) if resp_body else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:500]
        except Exception:
            pass
        return {"_error": True, "status_code": e.code, "reason": e.reason, "detail": detail}
    except Exception as e:
        return {"_error": True, "reason": str(e)}


def _get_org_id(env_id: str) -> str:
    """Get org_id for environment. Hardcoded for some, discovered for others."""
    env = ENVIRONMENTS[env_id]
    if "org_id" in env:
        return env["org_id"]
    # DEV Global: discover from /organizations
    r = _api(env_id, "GET", "/organizations")
    if isinstance(r, list) and r:
        return r[0].get("id", r[0].get("_id", ""))
    if isinstance(r, dict):
        items = r.get("data", r.get("organizations", []))
        if isinstance(items, list) and items:
            return items[0].get("id", items[0].get("_id", ""))
    return "6981cd9db88add48f0e782e3"  # fallback DEV Global


def _build_multipart(fields: dict) -> tuple:
    """Build multipart/form-data body. Returns (body_bytes, content_type)."""
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        parts.append(f"{v}\r\n".encode())
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _find_worker_by_name(env_id: str, name: str) -> dict:
    """Search for a worker-manager by name across all workspaces. Returns wm dict or error dict."""
    env = ENVIRONMENTS[env_id]
    if env.get("auth_type") == "bearer":
        # No org/workspace - search in /workers/me
        r = _api(env_id, "GET", "/workers/me")
        if r.get("_error"):
            return r
        items = r.get("data", [])
        if not isinstance(items, list):
            return {"_error": True, "reason": "Respuesta inesperada"}
        name_low = name.lower()
        for wm in items:
            if (wm.get("name", "") or "").lower() == name_low:
                return wm
        partial = [wm for wm in items if name_low in (wm.get("name", "") or "").lower()]
        if len(partial) == 1:
            return partial[0]
        if partial:
            return {"_error": True, "reason": f"Multiples workers coinciden: {[w.get('name') for w in partial[:5]]}"}
        return {"_error": True, "reason": f"Worker '{name}' no encontrado"}
    org_id = _get_org_id(env_id)
    rw = _api(env_id, "GET", f"/organizations/{org_id}/workspaces?limit=100")
    ws_list = []
    if not rw.get("_error"):
        raw = rw.get("data", rw)
        ws_list = raw if isinstance(raw, list) else (raw.get("items", raw.get("workspaces", [])) if isinstance(raw, dict) else [])

    name_low = name.lower()
    found = []
    exact = [None]

    def _search_ws(ws):
        ws_id = ws.get("id", ws.get("_id", ""))
        matches = []
        page = 1
        while page <= 50:
            if exact[0]:
                return matches
            r2 = _api(env_id, "GET", f"/organizations/{org_id}/workspaces/{ws_id}/worker-managers?limit=100&page={page}")
            items = r2.get("data", [])
            if isinstance(items, dict):
                items = items.get("items", items.get("workerManagers", []))
            if not isinstance(items, list) or not items:
                break
            for wm in items:
                wm["_ws_id"] = ws_id
                wm_name = (wm.get("name", "") or "").lower()
                if wm_name == name_low:
                    exact[0] = wm
                    return [wm]
                if name_low in wm_name:
                    matches.append(wm)
            pag = r2.get("pagination", {})
            if not isinstance(pag, dict) or not pag.get("hasNextPage", False):
                break
            page += 1
        return matches

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_search_ws, ws): ws for ws in ws_list}
        for f in as_completed(futures):
            if exact[0]:
                break
            res = f.result()
            if res:
                found.extend(res)

    if exact[0]:
        return exact[0]
    if not found:
        return {"_error": True, "reason": f"Worker '{name}' no encontrado"}
    if len(found) == 1:
        return found[0]
    ex = [w for w in found if (w.get("name", "") or "").lower() == name_low]
    if len(ex) == 1:
        return ex[0]
    names = ", ".join(w.get("name", "?") for w in found[:5])
    return {"_error": True, "reason": f"Multiples coincidencias: {names}"}


def _is_hex_id(s: str) -> bool:
    return len(s) >= 20 and all(c in "0123456789abcdef" for c in s.lower())


def _resolve_worker(env_id: str, inp: str) -> dict:
    """Resolve worker name/ID to worker-manager detail + instructions.
    Returns dict with wm_id, last_vid, dep_vid, name, instructions, knowhow, totalVersions, totalRuns, or _error."""
    if _is_hex_id(inp):
        wm_id = inp
    else:
        found = _find_worker_by_name(env_id, inp)
        if found.get("_error"):
            return found
        wm_id = found.get("id", found.get("_id", ""))

    r = _api(env_id, "GET", f"/worker-manager/{wm_id}")
    if r.get("_error"):
        return r
    wmd = r.get("data", r)
    if not isinstance(wmd, dict):
        return {"_error": True, "reason": "Respuesta inesperada de worker-manager"}

    last_vid = wmd.get("lastVersionId", "")
    dep_vid = wmd.get("deployedVersionId", "")
    wname = wmd.get("name", "")
    total_versions = max(1, wmd.get("totalVersions", 1) or 1)
    total_runs = wmd.get("totalRuns", 0) or 0

    # Extract instructions from nested structures
    wk_instructions = ""
    wk_knowhow = ""
    _instr_keys = ("instructions", "query", "prompt", "jobToBeDone")
    _kh_keys = ("knowHow", "knowhow", "knowledge", "knowHows")
    for sub in [wmd, wmd.get("latestVersion", {}), wmd.get("worker", {}), wmd.get("latestWorker", {})]:
        if isinstance(sub, dict):
            if not wk_instructions:
                for ik in _instr_keys:
                    v = sub.get(ik)
                    if v:
                        wk_instructions = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                        break
            if not wk_knowhow:
                for kk in _kh_keys:
                    v = sub.get(kk)
                    if v:
                        if isinstance(v, list):
                            wk_knowhow = " ".join(str(x) for x in v)
                        elif isinstance(v, str):
                            wk_knowhow = v
                        else:
                            wk_knowhow = json.dumps(v, ensure_ascii=False)
                        break

    # Fallback: fetch worker detail for instructions
    if not wk_instructions and (last_vid or wm_id):
        wr = _api(env_id, "GET", f"/workers/{last_vid or wm_id}")
        if not wr.get("_error"):
            wkd = wr.get("data", wr)
            if isinstance(wkd, dict):
                for ik in _instr_keys:
                    if not wk_instructions and wkd.get(ik):
                        val = wkd[ik]
                        wk_instructions = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False) if val else ""
                        break
                for kk in _kh_keys:
                    if not wk_knowhow and wkd.get(kk):
                        val = wkd[kk]
                        if isinstance(val, list):
                            wk_knowhow = " ".join(str(x) for x in val)
                        elif isinstance(val, str):
                            wk_knowhow = val
                        else:
                            wk_knowhow = json.dumps(val, ensure_ascii=False) if val else ""
                        break

    return {
        "wm_id": wm_id, "last_vid": last_vid, "dep_vid": dep_vid,
        "name": wname, "instructions": wk_instructions, "knowhow": wk_knowhow,
        "totalVersions": total_versions, "totalRuns": total_runs,
    }


def _discover_versions(env_id: str, wm_id: str, last_vid: str, dep_vid: str) -> list:
    """Discover all version IDs for a worker-manager. Same logic as report_v2."""
    ids_to_try = list(dict.fromkeys([i for i in [last_vid, dep_vid] if i]))
    vid_for_versions = last_vid or dep_vid
    if vid_for_versions:
        ver_resp = _api(env_id, "GET", f"/workers/{vid_for_versions}/versions?limit=100")
        if not ver_resp.get("_error"):
            ver_data = ver_resp.get("data", ver_resp)
            if isinstance(ver_data, list):
                for v in ver_data:
                    if isinstance(v, dict):
                        vid = v.get("id", "")
                        if vid and vid not in ids_to_try:
                            ids_to_try.append(vid)
    # Fallback: infer first version (wmId+1)
    if len(ids_to_try) <= 1 and wm_id and len(wm_id) == 24:
        try:
            first_ver = format(int(wm_id, 16) + 1, '024x')
            if first_ver not in ids_to_try:
                ids_to_try.append(first_ver)
        except ValueError:
            pass
    return ids_to_try


def _aggregate_tokens(env_id: str, version_ids: list, fetch_execs: bool = False) -> dict:
    """Fetch totalConsumption from /executions/worker/{vid} for each version and aggregate.
    Returns {totalTokens, tokensByModel: {model: {in, out}}, executions: [...]}.
    If fetch_execs=True, also returns ALL deduplicated executions (paginated with cursor=).
    Same logic as report_v2."""
    total_tokens = 0
    tokens_by_model = {}
    all_execs = []
    seen_ids = set()
    lim = "100" if fetch_execs else "1"
    for vid in version_ids:
        cursor = ""
        first_page = True
        while True:
            url = f"/executions/worker/{vid}?limit={lim}&showAll=true"
            if cursor:
                from urllib.parse import quote
                url += f"&cursor={quote(cursor, safe='')}"
            r = _api(env_id, "GET", url)
            if r.get("_error"):
                break
            data = r.get("data", r)
            if first_page:
                tc = data.get("totalConsumption", {}) if isinstance(data, dict) else {}
                if isinstance(tc, dict):
                    total_tokens += tc.get("totalServicesTokens", 0)
                    for svc in tc.get("services", []):
                        if not isinstance(svc, dict):
                            continue
                        for m in svc.get("models", []):
                            if not isinstance(m, dict):
                                continue
                            mn = m.get("friendlyName") or m.get("name", "")
                            if not mn.strip():
                                mn = "?"
                            if mn not in tokens_by_model:
                                tokens_by_model[mn] = {"in": 0, "out": 0}
                            tokens_by_model[mn]["in"] += m.get("input", 0)
                            tokens_by_model[mn]["out"] += m.get("output", 0)
                first_page = False
            if fetch_execs:
                execs = data.get("executions", []) if isinstance(data, dict) else []
                for ex in execs:
                    eid = ex.get("id", "")
                    if eid and eid not in seen_ids:
                        seen_ids.add(eid)
                        all_execs.append(ex)
            if not fetch_execs:
                break
            pag = r.get("pagination", {})
            cursor = pag.get("nextPageToken", "")
            if not pag.get("hasNextPage", False) or not cursor:
                break
    return {"totalTokens": total_tokens, "tokensByModel": tokens_by_model, "executions": all_execs}


# ── Tool definitions ─────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "maisa_env",
        "description": f"Muestra el entorno Maisa Studio activo y los {len(ENVIRONMENTS)} entornos disponibles agrupados por entidad (Argentina, Brasil, Chile, CIB, Colombia, Germany, Global Cards, HQ P&C, Mexico, Platform, SCF Global, SDS, Spain, UK, Uruguay, USA, WM&I). Indica si cada uno tiene cookie de sesion.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "maisa_switch_env",
        "description": f"Cambia el entorno activo de Maisa Studio. {len(ENVIRONMENTS)} entornos disponibles. Usa maisa_env para ver la lista completa.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "env_id": {"type": "string", "description": f"ID del entorno. Ejemplos: dev_global, dev_spain, pro_brasil, argentina_dev, chile_pro, uk_pre. Usa maisa_env para ver todos los {len(ENVIRONMENTS)} entornos."},
            },
            "required": ["env_id"],
        },
    },
    {
        "name": "maisa_list_workspaces",
        "description": "Lista los workspaces disponibles en la organizacion del entorno Maisa Studio activo.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "maisa_list_workers",
        "description": "Lista los Digital Workers de un workspace en Maisa Studio. Acepta nombre o ID de workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Nombre o ID del workspace"},
                "page": {"type": "integer", "description": "Numero de pagina (default 1)"},
            },
            "required": ["workspace"],
        },
    },
    {
        "name": "maisa_get_worker",
        "description": "Obtiene detalles de un Digital Worker en Maisa Studio. Acepta nombre o worker-manager ID. Devuelve nombre, estado, modelo, total de ejecuciones, versiones, etc.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {"type": "string", "description": "Nombre o worker-manager ID del worker"},
            },
            "required": ["worker"],
        },
    },
    {
        "name": "maisa_get_executions",
        "description": "Lista las ultimas ejecuciones de un Digital Worker con metricas Phase 1 (Time Freed). Descubre todas las versiones, enriquece con tokens, ejecuta profiler heuristico y calcula T_h/TF por ejecucion. Acepta nombre o worker-manager ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {"type": "string", "description": "Nombre o worker ID del worker"},
                "limit": {"type": "integer", "description": "Numero maximo de ejecuciones (default 20, max 100)"},
            },
            "required": ["worker"],
        },
    },
    {
        "name": "maisa_get_execution",
        "description": "Obtiene el detalle de una ejecucion individual por su execution ID. Devuelve estado, resultado, variables de entrada, modelo, tiempo, etc.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "execution_id": {"type": "string", "description": "ID de la ejecucion"},
            },
            "required": ["execution_id"],
        },
    },
    {
        "name": "maisa_create_worker",
        "description": "Crea un nuevo Digital Worker en Maisa Studio. Requiere workspace, nombre y prompt (instrucciones). El worker se crea en estado DRAFT.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Nombre o ID del workspace"},
                "name": {"type": "string", "description": "Nombre del worker (3-50 chars)"},
                "prompt": {"type": "string", "description": "Instrucciones/prompt para el worker"},
            },
            "required": ["workspace", "name", "prompt"],
        },
    },
    {
        "name": "maisa_deploy_worker",
        "description": "Despliega (publica) un Digital Worker para que pueda ser ejecutado. Acepta nombre o worker version ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {"type": "string", "description": "Nombre o version ID del worker a desplegar"},
            },
            "required": ["worker"],
        },
    },
    {
        "name": "maisa_worker_profiler",
        "description": "Ejecuta el profiler heuristico sobre un Digital Worker. Analiza sus instrucciones y know-how para clasificar categoria, complejidad, pesos y riesgos. Acepta nombre o worker-manager ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {"type": "string", "description": "Nombre o worker-manager ID del worker"},
            },
            "required": ["worker"],
        },
    },
    {
        "name": "maisa_run_worker",
        "description": "Ejecuta un Digital Worker desplegado en Maisa Studio. Acepta nombre o worker-manager ID. Opcionalmente recibe variables de entrada como objeto JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {"type": "string", "description": "Nombre o worker-manager ID del worker"},
                "input_variables": {
                    "type": "object",
                    "description": "Variables de entrada como pares clave-valor. Ejemplo: {\"num1\": \"5\", \"num2\": \"3\"}",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["worker"],
        },
    },
    {
        "name": "maisa_dashboard",
        "description": "Obtiene el resumen del dashboard de todos los entornos desde el servidor local (localhost:9090). Muestra runs, workers, workspaces, actividad diaria, tokens y logins por entorno.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "maisa_splunk_logins",
        "description": "Obtiene datos de logins de Splunk (Azure AD sign-in) agregados por entorno Maisa. Muestra total logins, usuarios unicos y desglose por app/entorno.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "maisa_add_member",
        "description": "Agrega un usuario como miembro (ws_manager) a un workspace. Util para obtener acceso a workspaces donde solo se tiene acceso heredado (ws_parent_admin). Requiere el email del usuario y el workspace ID o nombre. Primero resuelve el userId del usuario via /organizations/{org}/members y luego lo anade con POST /organizations/{org}/workspaces/{ws}/members usando el formato [{{userId, role}}].",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "ID del workspace"},
                "email": {"type": "string", "description": "Email del usuario a agregar"},
                "role": {
                    "type": "string",
                    "description": "Rol: ws_manager (default), ws_creator, ws_operator",
                    "enum": ["ws_manager", "ws_creator", "ws_operator"],
                },
            },
            "required": ["workspace_id", "email"],
        },
    },
]

# ── Tool dispatcher ──────────────────────────────────────────────────────────

def _text(msg: str) -> list:
    return [{"type": "text", "text": msg}]


def handle_tool(name: str, args: dict) -> list:
    """Execute a tool and return MCP content list."""
    global _current_env
    try:
        # ── maisa_env ──
        if name == "maisa_env":
            lines = [f"Entorno activo: {_current_env} ({ENVIRONMENTS[_current_env]['label']})"]
            lines.append(f"API: {ENVIRONMENTS[_current_env]['api_base']}")
            cookie = _load_cookie(_current_env)
            lines.append(f"Sesion: {'Activa' if cookie else 'Sin cookie'}")
            lines.append("")
            lines.append(f"Entornos disponibles ({len(ENVIRONMENTS)}):")
            # Group by entity
            from collections import OrderedDict
            groups = OrderedDict()
            for eid, env in ENVIRONMENTS.items():
                g = env.get("label", "").rsplit(" ", 1)[0] if " " in env.get("label", "") else "Other"
                if g not in groups:
                    groups[g] = []
                groups[g].append((eid, env))
            for grp, envs in sorted(groups.items()):
                lines.append(f"  {grp}:")
                for eid, env in envs:
                    c = _load_cookie(eid)
                    marker = " <- activo" if eid == _current_env else ""
                    tier = env["label"].rsplit(" ", 1)[-1] if " " in env["label"] else env["label"]
                    status = "OK" if c else "--"
                    lines.append(f"    {tier:5s} {eid:20s} [{status}]{marker}")
            return _text("\n".join(lines))

        # ── maisa_switch_env ──
        if name == "maisa_switch_env":
            eid = args["env_id"]
            if eid not in ENVIRONMENTS:
                return _text(f"Error: entorno '{eid}' no valido. Usa: {', '.join(ENVIRONMENTS.keys())}")
            _current_env = eid
            cookie = _load_cookie(eid)
            return _text(f"Entorno cambiado a {ENVIRONMENTS[eid]['label']}\nAPI: {ENVIRONMENTS[eid]['api_base']}\nSesion: {'Activa' if cookie else 'Sin cookie - autenticate en la UI (localhost:9090)'}")

        # ── maisa_list_workspaces ──
        if name == "maisa_list_workspaces":
            env = ENVIRONMENTS[_current_env]
            if env.get("auth_type") == "bearer":
                # No org/workspace structure - list workers via /workers/me
                r = _api(_current_env, "GET", "/workers/me")
                if r.get("_error"):
                    return _text(f"Error: {r.get('reason', 'unknown')}")
                workers = r.get("data", [])
                cnt = len(workers) if isinstance(workers, list) else 0
                lines = [f"1 workspace(s) en {env['label']} (sin estructura org/workspace):", ""]
                lines.append(f"{'_all':26s} {'Workers':30s} ({cnt} workers)")
                return _text("\n".join(lines))
            org_id = _get_org_id(_current_env)
            r = _api(_current_env, "GET", f"/organizations/{org_id}/workspaces?limit=100")
            if r.get("_error"):
                return _text(f"Error: {r.get('reason', 'unknown')}")
            raw = r.get("data", r)
            ws_list = raw if isinstance(raw, list) else (raw.get("items", raw.get("workspaces", [])) if isinstance(raw, dict) else [])
            lines = [f"{len(ws_list)} workspace(s) en {ENVIRONMENTS[_current_env]['label']}:", ""]
            lines.append(f"{'ID':26s} {'NOMBRE':30s}")
            lines.append(f"{'─'*26} {'─'*30}")
            for ws in ws_list:
                wid = ws.get("id", ws.get("_id", "?"))
                wname = ws.get("name", "?")
                lines.append(f"{wid:26s} {wname:30s}")
            return _text("\n".join(lines))

        # ── maisa_list_workers ──
        if name == "maisa_list_workers":
            env = ENVIRONMENTS[_current_env]
            if env.get("auth_type") == "bearer":
                # No org/workspace - list all workers via /workers/me
                r = _api(_current_env, "GET", "/workers/me")
                if r.get("_error"):
                    return _text(f"Error: {r.get('reason', 'unknown')}")
                items = r.get("data", [])
                if not isinstance(items, list):
                    items = []
                lines = [f"{len(items)} worker(s):", ""]
                lines.append(f"{'WMID':26s} {'NOMBRE':34s} {'STATUS':10s} {'RUNS':>6s}")
                lines.append(f"{'─'*26} {'─'*34} {'─'*10} {'─'*6}")
                for wm in items:
                    wmid = wm.get("workerManagerId", wm.get("id", "?"))
                    wname = (wm.get("name", "?") or "?")[:34]
                    status = wm.get("status", "?")
                    runs = str(wm.get("executionCount", 0))
                    lines.append(f"{wmid:26s} {wname:34s} {status:10s} {runs:>6s}")
                return _text("\n".join(lines))
            org_id = _get_org_id(_current_env)
            ws_input = args["workspace"]
            page = args.get("page", 1)
            # Resolve workspace
            ws_id = ws_input
            if not _is_hex_id(ws_input):
                rw = _api(_current_env, "GET", f"/organizations/{org_id}/workspaces?limit=100")
                raw = rw.get("data", rw) if not rw.get("_error") else []
                wsl = raw if isinstance(raw, list) else (raw.get("items", raw.get("workspaces", [])) if isinstance(raw, dict) else [])
                match = [w for w in wsl if ws_input.lower() in (w.get("name", "") or "").lower()]
                if not match:
                    return _text(f"Workspace '{ws_input}' no encontrado")
                if len(match) > 1:
                    exact = [w for w in match if (w.get("name", "") or "").lower() == ws_input.lower()]
                    match = exact if exact else match
                if len(match) > 1:
                    return _text(f"Multiples workspaces. Se mas preciso.")
                ws_id = match[0].get("id", match[0].get("_id", ""))
            r = _api(_current_env, "GET", f"/organizations/{org_id}/workspaces/{ws_id}/worker-managers?limit=100&page={page}")
            if r.get("_error"):
                return _text(f"Error: {r.get('reason', 'unknown')}")
            items = r.get("data", [])
            if isinstance(items, dict):
                items = items.get("items", items.get("workerManagers", []))
            if not isinstance(items, list):
                items = []
            lines = [f"{len(items)} worker(s) - pagina {page}:", ""]
            lines.append(f"{'WMID':26s} {'NOMBRE':34s} {'STATUS':10s} {'RUNS':>6s}")
            lines.append(f"{'─'*26} {'─'*34} {'─'*10} {'─'*6}")
            for wm in items:
                wmid = wm.get("id", wm.get("_id", "?"))
                wname = (wm.get("name", "?") or "?")[:34]
                status = wm.get("status", "?")
                runs = str(wm.get("totalRuns", wm.get("runs", 0)))
                lines.append(f"{wmid:26s} {wname:34s} {status:10s} {runs:>6s}")
            return _text("\n".join(lines))

        # ── maisa_get_worker ──
        if name == "maisa_get_worker":
            inp = args["worker"]
            if _is_hex_id(inp):
                org_id = _get_org_id(_current_env)
                r = _api(_current_env, "GET", f"/worker-manager/{inp}")
                if r.get("_error"):
                    r = _api(_current_env, "GET", f"/workers/{inp}")
                if r.get("_error"):
                    return _text(f"Error: {r.get('reason', 'unknown')}")
                data = r.get("data", r)
            else:
                found = _find_worker_by_name(_current_env, inp)
                if found.get("_error"):
                    return _text(f"Error: {found.get('reason', 'unknown')}")
                data = found
            if not isinstance(data, dict):
                return _text(f"Respuesta inesperada: {str(data)[:500]}")
            fields = ["name", "id", "_id", "status", "deployedVersionId", "lastVersionId",
                       "totalRuns", "totalVersions", "model", "createdAt", "updatedAt",
                       "lastTimeRunned", "_ws_id"]
            lines = ["Worker Detail:", ""]
            for f in fields:
                v = data.get(f)
                if v is not None:
                    lines.append(f"  {f:24s} {str(v)}")
            # Aggregate tokens from all versions (same logic as report_v2)
            wm_id = data.get("id", data.get("_id", ""))
            last_vid = data.get("lastVersionId", "")
            dep_vid = data.get("deployedVersionId", "")
            version_ids = _discover_versions(_current_env, wm_id, last_vid, dep_vid)
            tok = _aggregate_tokens(_current_env, version_ids)
            lines.append("")
            lines.append(f"  {'versions_found':24s} {len(version_ids)}")
            lines.append(f"  {'totalTokens':24s} {tok['totalTokens']:,}")
            if tok["tokensByModel"]:
                for mn, vals in sorted(tok["tokensByModel"].items(), key=lambda x: x[1]["in"]+x[1]["out"], reverse=True):
                    lines.append(f"    {mn:22s} in:{vals['in']:,}  out:{vals['out']:,}  total:{vals['in']+vals['out']:,}")
            return _text("\n".join(lines))

        # ── maisa_get_executions ──
        if name == "maisa_get_executions":
            inp = args["worker"]
            limit = min(args.get("limit", 20), 100)

            # 1) Resolve worker -> worker-manager detail + instructions
            winfo = _resolve_worker(_current_env, inp)
            if winfo.get("_error"):
                return _text(f"Error: {winfo.get('reason', 'unknown')}")

            wm_id = winfo["wm_id"]
            last_vid = winfo["last_vid"]
            dep_vid = winfo["dep_vid"]
            wname = winfo["name"]

            # 2) Run profiler
            profiler_result = _run_heuristic_profiler(winfo["instructions"], winfo["knowhow"])

            # 3) Discover all version IDs (shared logic)
            ids_to_try = _discover_versions(_current_env, wm_id, last_vid, dep_vid)

            # 3b) Aggregate tokens + fetch executions in single pass (same as report_v2)
            tok_agg = _aggregate_tokens(_current_env, ids_to_try, fetch_execs=True)
            all_execs = tok_agg["executions"]

            # 5) Fetch token details — skip error-status execs (BFF returns 403, no tokens)
            enrichable = [ex for ex in all_execs if ex.get("status") != "error"]
            skipped = len(all_execs) - len(enrichable)
            log(f"Enriching {len(enrichable)} executions ({skipped} skipped with status=error)")

            def _fetch_tokens(ex):
                eid = ex.get("id", "")
                if not eid:
                    return
                try:
                    detail = _api(_current_env, "GET", f"/executions/{eid}")
                    if not detail.get("_error"):
                        dd = detail.get("data", detail)
                        if isinstance(dd, dict):
                            ex["tokenConsumptionTotals"] = dd.get("tokenConsumptionTotals", {})
                            ex["totalConsumption"] = dd.get("totalConsumption", {})
                            ex["steps"] = dd.get("steps", [])
                            if not ex.get("timeSpent") and dd.get("timeSpent"):
                                ex["timeSpent"] = dd["timeSpent"]
                except Exception:
                    pass

            # Pass 1: 15 threads
            with ThreadPoolExecutor(max_workers=15) as pool:
                list(pool.map(_fetch_tokens, enrichable))

            # Retry passes for transient failures
            import time as _time
            for retry in range(2):
                failed = [ex for ex in enrichable if ex.get("id") and not ex.get("tokenConsumptionTotals")]
                if not failed:
                    break
                _time.sleep(1)
                log(f"Retry {retry+1}: {len(failed)} without tokens, retrying (5 threads)")
                with ThreadPoolExecutor(max_workers=5) as pool:
                    list(pool.map(_fetch_tokens, failed))

            ok_count = sum(1 for ex in all_execs if ex.get("tokenConsumptionTotals"))
            log(f"Token enrichment done: {ok_count}/{len(enrichable)} enrichable, {skipped} error-status skipped")

            # 6) Build output with Phase 1
            all_execs.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
            total_th = 0.0
            total_tf = 0.0
            p1_count = 0

            lines = [f"Worker: {wname} (wmid: {wm_id})"]
            lines.append(f"Versiones: {winfo['totalVersions']}, Runs WM: {winfo['totalRuns']}, Encontradas: {len(all_execs)}")
            lines.append(f"Tokens (agregado servidor): {tok_agg['totalTokens']:,}")
            if tok_agg["tokensByModel"]:
                for mn, vals in sorted(tok_agg["tokensByModel"].items(), key=lambda x: x[1]["in"]+x[1]["out"], reverse=True):
                    lines.append(f"  {mn}: in:{vals['in']:,} out:{vals['out']:,}")
            if profiler_result:
                lines.append(f"Profiler: cat={profiler_result['task_category']}, complexity={profiler_result['complexity_bucket']}, "
                             f"w_out={profiler_result['weights']['w_out']}, w_in={profiler_result['weights']['w_in']}")
            lines.append("")
            lines.append(f"{'ID':26s} {'STATUS':10s} {'T_h':>8s} {'TF':>8s} {'Tok IN':>8s} {'Tok OUT':>8s} {'TIEMPO':>8s} {'FECHA':20s}")
            lines.append(f"{'─'*26} {'─'*10} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*20}")

            display_execs = all_execs[:limit]
            for ex in display_execs:
                eid = ex.get("id", "?")
                st = ex.get("status", "?")
                totals = ex.get("tokenConsumptionTotals", {}) or {}
                tok_in = totals.get("inputTokens")
                tok_out = totals.get("outputTokens")
                p1 = _calc_phase1(profiler_result, tok_in, tok_out)
                th_str = f"{p1['th_minutes']:.1f}m" if p1 else "-"
                tf_str = f"{p1['tf_minutes']:.1f}m" if p1 else "-"
                if p1:
                    total_th += p1["th_minutes"]
                    total_tf += p1["tf_minutes"]
                    p1_count += 1
                tok_in_str = str(tok_in) if tok_in is not None else "-"
                tok_out_str = str(tok_out) if tok_out is not None else "-"
                ts = ex.get("timeSpent", "")
                ts_str = f"{ts}s" if ts else "-"
                dt = str(ex.get("createdAt", ""))[:19]
                lines.append(f"{eid:26s} {st:10s} {th_str:>8s} {tf_str:>8s} {tok_in_str:>8s} {tok_out_str:>8s} {ts_str:>8s} {dt:20s}")

            if p1_count > 0:
                lines.append("")
                lines.append(f"── Phase 1 Totals ({p1_count} ejecuciones con tokens) ──")
                lines.append(f"  T_h total: {total_th:.1f} min ({total_th/60:.1f} h)")
                lines.append(f"  TF total:  {total_tf:.1f} min ({total_tf/60:.1f} h)")
                lines.append(f"  TF medio:  {total_tf/p1_count:.1f} min/ejecucion")
                lines.append(f"  h_global:  {H_GLOBAL}")

            return _text("\n".join(lines))

        # ── maisa_get_execution ──
        if name == "maisa_get_execution":
            eid = args["execution_id"]
            r = _api(_current_env, "GET", f"/executions/{eid}")
            if r.get("_error"):
                return _text(f"Error: {r.get('reason', 'unknown')}")
            data = r.get("data", r)
            if not isinstance(data, dict):
                return _text(f"Respuesta inesperada: {str(data)[:500]}")
            lines = ["Execution Detail:", ""]
            for f in ["id", "status", "model", "timeSpent", "createdAt", "updatedAt", "workerId", "userName"]:
                v = data.get(f)
                if v is not None:
                    lines.append(f"  {f:22s} {str(v)}")
            # Token consumption
            totals = data.get("tokenConsumptionTotals", {}) or {}
            if totals:
                lines.append("  tokens:")
                lines.append(f"    input:  {totals.get('inputTokens', '-')}")
                lines.append(f"    output: {totals.get('outputTokens', '-')}")
                lines.append(f"    total:  {totals.get('totalTokens', '-')}")
            # Services breakdown
            tc = data.get("totalConsumption", {}) or {}
            for svc in (tc.get("services", []) or []):
                if isinstance(svc, dict):
                    for m in (svc.get("models", []) or []):
                        if isinstance(m, dict):
                            lines.append(f"    {svc.get('friendlyName', svc.get('key', ''))}/{m.get('friendlyName', m.get('name', ''))}: in={m.get('input', 0)} out={m.get('output', 0)}")
            iv = data.get("inputVariables", [])
            if iv:
                lines.append("  inputVariables:")
                for inp_var in iv:
                    if isinstance(inp_var, dict):
                        lines.append(f"    {inp_var.get('name', '?')} = {str(inp_var.get('value', ''))[:200]}")
            result = data.get("result", "")
            if result:
                lines.append(f"  result:")
                lines.append(f"    {str(result)[:2000]}")
            return _text("\n".join(lines))

        # ── maisa_worker_profiler ──
        if name == "maisa_worker_profiler":
            inp = args["worker"]
            winfo = _resolve_worker(_current_env, inp)
            if winfo.get("_error"):
                return _text(f"Error: {winfo.get('reason', 'unknown')}")
            instructions = winfo["instructions"]
            knowhow = winfo["knowhow"]
            wname = winfo["name"]
            wmid = winfo["wm_id"]
            if not instructions and not knowhow:
                return _text(f"Worker '{wname}' no tiene instrucciones ni know-how para analizar.")
            # Try LLM profiler first, fall back to heuristic
            pf = _run_llm_profiler(instructions, knowhow, agent_id=wmid, agent_name=wname)
            profiler_type = "LLM (v0.1)"
            if not pf:
                pf = _run_heuristic_profiler(instructions, knowhow)
                profiler_type = "heuristic (v0.1)"
            if not pf:
                return _text(f"No se pudo clasificar el worker '{wname}'.")
            tlen = len((instructions + " " + knowhow).strip())
            # Use risks from profiler (spec §4 schema)
            risks = pf.get("risks", {})
            active_risks = [k for k, v in risks.items() if v]
            lines = [f"Profiler {profiler_type}: {wname} (wmid: {wmid})", ""]
            lines.append(f"  Task category:    {pf.get('task_category')}")
            if pf.get("task_category_rationale"):
                lines.append(f"  Rationale:        {pf['task_category_rationale']}")
            lines.append(f"  Complexity:       {pf.get('complexity_bucket')}")
            if pf.get("complexity_rationale"):
                lines.append(f"  Rationale:        {pf['complexity_rationale']}")
            w = pf.get("weights", {})
            lines.append(f"  Weights:          w_out={w.get('w_out',0)}, w_in={w.get('w_in',0)}, w_fixed={w.get('w_fixed',0)}")
            if pf.get("weights_rationale"):
                lines.append(f"  Rationale:        {pf['weights_rationale']}")
            lines.append(f"  Instructions:     {len(instructions)} chars")
            lines.append(f"  Know-how:         {len(knowhow)} chars")
            lines.append("")
            # Category rate info (from governed objects v0.1)
            rate = CATEGORY_RATES.get(pf.get("task_category"), {})
            if rate:
                lines.append(f"  Category rates:   rho_out={rate['rho_out']} tok/min, rho_in={rate['rho_in']} tok/min, tau_fixed={rate['tau_fixed']} min")
                lines.append(f"                    output_cap={rate['output_cap_tok']} tok")
            m_b = COMPLEXITY_MULTIPLIERS.get(pf.get("complexity_bucket"), 1.0)
            lines.append(f"  Multiplier m(b):  {m_b}")
            lines.append(f"  h_global:         {H_GLOBAL}")
            lines.append("")
            lines.append(f"  Risks:            {', '.join(active_risks) if active_risks else 'ninguno detectado'}")
            # Inferred attributes (spec §4)
            for attr in ("inferred_process", "inferred_domain", "inferred_global_business"):
                inf = pf.get(attr)
                if inf and isinstance(inf, dict):
                    label = inf.get("label") or inf.get("code") or "null"
                    conf = inf.get("confidence", "low")
                    lines.append(f"  {attr}: {label} (confidence={conf}, status=draft)")
            if pf.get("notes"):
                lines.append(f"  Notes:            {pf['notes']}")
            lines.append(f"  Profiler version: {pf.get('profiler_version', 'v0.1')}")
            if pf.get("_model"):
                lines.append(f"  Model:            {pf['_model']} ({pf.get('_tokens', 0)} tokens)")
            return _text("\n".join(lines))

        # ── maisa_create_worker ──
        if name == "maisa_create_worker":
            org_id = _get_org_id(_current_env)
            ws_input = args["workspace"]
            wname = args["name"]
            prompt = args["prompt"]
            # Resolve workspace
            ws_id = ws_input
            if not _is_hex_id(ws_input):
                rw = _api(_current_env, "GET", f"/organizations/{org_id}/workspaces?limit=100")
                raw = rw.get("data", rw) if not rw.get("_error") else []
                wsl = raw if isinstance(raw, list) else (raw.get("items", raw.get("workspaces", [])) if isinstance(raw, dict) else [])
                match = [w for w in wsl if ws_input.lower() in (w.get("name", "") or "").lower()]
                if not match:
                    return _text(f"Workspace '{ws_input}' no encontrado")
                if len(match) > 1:
                    exact = [w for w in match if (w.get("name", "") or "").lower() == ws_input.lower()]
                    match = exact if exact else match
                if len(match) > 1:
                    return _text(f"Multiples workspaces. Se mas preciso o usa el ID.")
                ws_id = match[0].get("id", match[0].get("_id", ""))
            body, ct = _build_multipart({"name": wname, "query": prompt, "workspaceId": ws_id, "organizationId": org_id})
            r = _api(_current_env, "POST", f"/organizations/{org_id}/workspaces/{ws_id}/workers", body=body, content_type=ct)
            if r.get("_error"):
                return _text(f"Error creando worker: {r.get('reason', 'unknown')}")
            d = r.get("data", r)
            wid = d.get("id", "?") if isinstance(d, dict) else "?"
            wmid = d.get("workerManagerId", "?") if isinstance(d, dict) else "?"
            return _text(f"Worker creado!\n  ID: {wid}\n  WM ID: {wmid}\n  Nombre: {wname}\n\nSiguiente paso: maisa_deploy_worker(worker=\"{wid}\")")

        # ── maisa_deploy_worker ──
        if name == "maisa_deploy_worker":
            inp = args["worker"]
            wid = inp
            wname = inp
            if not _is_hex_id(inp):
                found = _find_worker_by_name(_current_env, inp)
                if found.get("_error"):
                    return _text(f"Error: {found.get('reason', 'unknown')}")
                wid = found.get("lastVersionId") or found.get("deployedVersionId") or found.get("id", found.get("_id", ""))
                wname = found.get("name", inp)
            r = _api(_current_env, "POST", f"/workers/{wid}/deploy",
                     body=json.dumps({}).encode(), content_type="application/json", timeout=60)
            if r.get("_error"):
                # Retry with checkExplainer
                r = _api(_current_env, "POST", f"/workers/{wid}/deploy",
                         body=json.dumps({"checkExplainer": True}).encode(), content_type="application/json", timeout=60)
            if r.get("_error"):
                return _text(f"Error deploy: {r.get('reason', 'unknown')}")
            return _text(f"Worker '{wname}' desplegado!\n  Version ID: {wid}")

        # ── maisa_run_worker ──
        if name == "maisa_run_worker":
            inp = args["worker"]
            ivars_dict = args.get("input_variables", {})
            wmid = inp
            ws_id_found = ""
            vid = ""
            if not _is_hex_id(inp):
                found = _find_worker_by_name(_current_env, inp)
                if found.get("_error"):
                    return _text(f"Error: {found.get('reason', 'unknown')}")
                wmid = found.get("id", found.get("_id", ""))
                ws_id_found = found.get("_ws_id", "")
                vid = found.get("deployedVersionId") or found.get("lastVersionId") or ""
            ivars = [{"name": k, "value": v} for k, v in ivars_dict.items()]
            body_mp, ct_mp = _build_multipart({"inputVariables": json.dumps(ivars)})
            # Try multiple run paths with multipart (JSON run gives 500)
            org_id = _get_org_id(_current_env)
            run_paths = []
            if vid:
                run_paths.append(f"/workers/{vid}/run")
            run_paths.append(f"/digital-worker/{wmid}/run")
            if ws_id_found:
                run_paths.append(f"/organizations/{org_id}/workspaces/{ws_id_found}/worker-managers/{wmid}/run")
            r = None
            for rp in run_paths:
                r = _api(_current_env, "POST", rp, body=body_mp, content_type=ct_mp, timeout=60)
                if not r.get("_error"):
                    break
            if r.get("_error"):
                return _text(f"Error run: {r.get('reason', 'unknown')}")
            d = r.get("data", r)
            exec_id = (d.get("executionId") or d.get("id") or "") if isinstance(d, dict) else ""
            if not exec_id:
                return _text(f"Worker ejecutado pero sin execution ID en respuesta.\nRespuesta: {json.dumps(d)[:300]}")
            # Poll for result (max ~60s)
            import time as _time
            result_text = ""
            final_status = "unknown"
            for _i in range(20):
                _time.sleep(3)
                re = _api(_current_env, "GET", f"/executions/{exec_id}", timeout=30)
                if re.get("_error"):
                    continue
                de = re.get("data", re)
                final_status = (de.get("status") or "").lower()
                if final_status in ("completed", "failed", "error"):
                    result_text = de.get("result", "") or ""
                    break
            lines = [f"Worker ejecutado!", f"  Execution ID: {exec_id}", f"  Status: {final_status}"]
            if result_text:
                lines.append(f"  Resultado:\n{result_text[:2000]}")
            else:
                lines.append(f"  (sin resultado tras polling)")
            return _text("\n".join(lines))

        # ── maisa_dashboard ──
        if name == "maisa_dashboard":
            try:
                r = urllib.request.urlopen("http://localhost:9090/api/all-envs", timeout=30)
                d = json.loads(r.read())
            except Exception as e:
                return _text(f"Error conectando al dashboard (localhost:9090): {e}")
            envs = d.get("envs", [])
            sl = d.get("splunkLogins", {})
            lines = ["=== Maisa Dashboard ==="]
            lines.append(f"Entornos: {len(envs)}")
            totals = {"runs": 0, "workers": 0, "ws": 0, "deployed": 0}
            for e in envs:
                totals["runs"] += e.get("runs", 0)
                totals["workers"] += e.get("workers", 0)
                totals["ws"] += e.get("workspaces", 0)
                totals["deployed"] += e.get("deployed", 0)
            lines.append(f"Runs totales: {totals['runs']:,}")
            lines.append(f"Workers: {totals['workers']:,}  |  Deployed: {totals['deployed']:,}")
            lines.append(f"Workspaces: {totals['ws']:,}")
            if sl:
                lines.append(f"\n--- Logins Splunk ---")
                lines.append(f"Hoy:  {sl.get('loginsToday',0):,} logins, {sl.get('usersToday',0):,} usuarios")
                lines.append(f"Ayer: {sl.get('loginsYesterday',0):,} logins, {sl.get('usersYesterday',0):,} usuarios")
                lines.append(f"Cache: {sl.get('fetched','?')}")
            lines.append(f"\n--- Top entornos por runs ---")
            top = sorted([e for e in envs if e.get("runs", 0)], key=lambda x: -x.get("runs", 0))[:15]
            for e in top:
                lt = e.get("loginsToday", 0)
                ly = e.get("loginsYesterday", 0)
                login_str = f"  logins hoy={lt} ayer={ly}" if (lt or ly) else ""
                lines.append(f"  {e['label']}: {e.get('runs',0):,} runs, {e.get('workers',0):,} workers{login_str}")
            return _text("\n".join(lines))

        # ── maisa_splunk_logins ──
        if name == "maisa_splunk_logins":
            try:
                from maisa_splunk import get_logins_by_env
                data = get_logins_by_env()
            except ImportError:
                return _text("Error: maisa_splunk no disponible")
            if not data.get("_ok"):
                return _text(f"Error: {data.get('_error', 'sin cache de logins')}")
            total = data.get("_total", {})
            lines = ["=== Logins Maisa (Splunk) ==="]
            lines.append(f"Hoy:  {total.get('logins_today',0):,} logins, {total.get('users_today',0):,} usuarios unicos")
            lines.append(f"Ayer: {total.get('logins_yesterday',0):,} logins, {total.get('users_yesterday',0):,} usuarios unicos")
            lines.append(f"Total (cache): {total.get('logins',0):,} logins, {total.get('unique_users',0):,} usuarios")
            lines.append(f"Cache: {data.get('_fetched','?')}")
            lines.append(f"\n--- Por entorno (hoy + ayer) ---")
            env_data = {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}
            sorted_envs = sorted(env_data.items(), key=lambda x: -(x[1].get("logins_today", 0) + x[1].get("logins_yesterday", 0)))
            for eid, v in sorted_envs:
                lt = v.get("logins_today", 0)
                ut = v.get("users_today", 0)
                ly = v.get("logins_yesterday", 0)
                uy = v.get("users_yesterday", 0)
                if lt or ly:
                    label = ENVIRONMENTS.get(eid, {}).get("label", eid)
                    lines.append(f"  {label}: hoy={lt} ({ut} usr) | ayer={ly} ({uy} usr)")
            unmapped = data.get("_unmapped", {})
            if unmapped:
                lines.append(f"\n--- Apps sin mapear ---")
                for app, v in sorted(unmapped.items(), key=lambda x: -x[1].get("logins", 0)):
                    lines.append(f"  {app}: {v.get('logins',0)} logins")
            return _text("\n".join(lines))

        # ── maisa_add_member ──
        if name == "maisa_add_member":
            ws_id = args["workspace_id"]
            email = args["email"]
            role = args.get("role", "ws_manager")
            org_id = _get_org_id(_current_env)
            # Resolve userId from email via org members listing
            page = 1
            user_id = None
            while page <= 10:
                mr = _api(_current_env, "GET", f"/organizations/{org_id}/members?limit=100&page={page}")
                if mr.get("_error"):
                    return _text(f"Error listando miembros: {mr.get('reason', mr.get('detail', ''))}")
                members = mr.get("data", [])
                if not members:
                    break
                for m in members:
                    if m.get("email", "").lower() == email.lower():
                        user_id = m.get("userId", m.get("id", ""))
                        break
                if user_id:
                    break
                pag = mr.get("pagination", {})
                if not pag.get("hasNextPage", False):
                    break
                page += 1
            if not user_id:
                return _text(f"Error: usuario '{email}' no encontrado en la organizacion del entorno {ENVIRONMENTS[_current_env]['label']}")
            # Add member with array format: [{userId, role}]
            body = json.dumps([{"userId": user_id, "role": role}]).encode("utf-8")
            r = _api(_current_env, "POST", f"/organizations/{org_id}/workspaces/{ws_id}/members", body=body)
            if r.get("_error"):
                return _text(f"Error al anadir miembro: HTTP {r.get('status_code','')} {r.get('reason','')} {r.get('detail','')}")
            added = r.get("data", [])
            if isinstance(added, list) and added:
                a = added[0]
                return _text(f"Miembro anadido exitosamente:\n  Email: {a.get('email','')}\n  Rol: {a.get('role','')}\n  Workspace: {a.get('workspaceId','')}\n  UserId: {a.get('userId','')}")
            return _text(f"Miembro anadido. Respuesta: {json.dumps(r, ensure_ascii=False)[:300]}")

        return _text(f"Herramienta desconocida: {name}")

    except Exception as exc:
        log(f"Error in {name}: {exc}")
        return _text(f"Error: {exc}")

# ── JSON-RPC / MCP stdio loop ───────────────────────────────────────────────

_bare_mode = False

def send(obj: dict):
    """Write a JSON-RPC response to stdout."""
    raw = json.dumps(obj)
    if _bare_mode:
        msg = raw + "\n"
    else:
        msg = f"Content-Length: {len(raw)}\r\n\r\n{raw}"
    sys.stdout.buffer.write(msg.encode("utf-8"))
    sys.stdout.buffer.flush()


def read_message() -> dict | None:
    """Read a JSON-RPC message from stdin. Supports both Content-Length framing and bare JSON."""
    global _bare_mode
    try:
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            # Content-Length framed (LSP style)
            if text.startswith("Content-Length:"):
                try:
                    length = int(text.split(":", 1)[1].strip())
                except ValueError:
                    continue
                # Skip remaining headers until blank line
                while True:
                    hdr = sys.stdin.buffer.readline().decode("utf-8", errors="replace").strip()
                    if hdr == "":
                        break
                body = sys.stdin.buffer.read(length)
                if not body:
                    return None
                return json.loads(body.decode("utf-8"))
            # Bare JSON per line (Windsurf style)
            if text.startswith("{"):
                try:
                    _bare_mode = True
                    return json.loads(text)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log(f"read_message error: {e}")
        return None


def main():
    # Force binary mode on Windows to avoid \r\n corruption
    if sys.platform == "win32":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    log("Maisa MCP Server v2.0 starting...")
    log(f"Python: {sys.executable}")
    log(f"CWD: {os.getcwd()}")
    log(f"Script: {__file__}")

    while True:
        try:
            msg = read_message()
            if msg is None:
                break

            req_id = msg.get("id")
            method = msg.get("method", "")
            params = msg.get("params", {})

            if method == "initialize":
                send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "maisa-studio-mcp", "version": "2.0.0"},
                    },
                })

            elif method in ("notifications/initialized", "notifications/cancelled"):
                pass

            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})

            elif method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments", {})
                log(f"tools/call: {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:200]})")
                content = handle_tool(tool_name, tool_args)
                is_error = any("Error:" in c.get("text", "") for c in content)
                send({"jsonrpc": "2.0", "id": req_id, "result": {"content": content, "isError": is_error}})

            elif method == "ping":
                send({"jsonrpc": "2.0", "id": req_id, "result": {}})

            else:
                if req_id is not None:
                    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})

        except Exception as e:
            log(f"main loop error: {e}")

    log("Maisa MCP Server stopped.")


if __name__ == "__main__":
    main()
