"""
marathon-app Backend
Polar AccessLink sync + full run storage service.
"""

import asyncio
import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POLAR_CLIENT_ID = os.environ.get("POLAR_CLIENT_ID", "")
POLAR_CLIENT_SECRET = os.environ.get("POLAR_CLIENT_SECRET", "")
POLAR_REDIRECT_URI = os.environ.get("POLAR_REDIRECT_URI", "")
SYNC_HOUR_UTC = int(os.environ.get("SYNC_HOUR_UTC", "3"))  # 03:00 UTC = 04:00/05:00 DE
HF_MAX = int(os.environ.get("HF_MAX", "182"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

POLAR_AUTH_URL = "https://flow.polar.com/oauth2/authorization"
POLAR_TOKEN_URL = "https://polarremote.com/v2/oauth2/token"
POLAR_API_BASE = "https://www.polaraccesslink.com"

TOKEN_FILE = DATA_DIR / "token.json"
RUNS_FILE = DATA_DIR / "all_runs.json"
DELETED_IDS_FILE = DATA_DIR / "deleted_ids.json"
STATE_FILE = DATA_DIR / "oauth_state.txt"
APP_STATE_FILE = DATA_DIR / "app_state.json"
LAST_SYNC_FILE = DATA_DIR / "last_sync.txt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hm-api")

# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.warning("Could not read %s: %s — returning default", path, e)
        return default

def _write_json(path: Path, data: Any) -> None:
    _ensure_data_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)

def load_token() -> dict | None:
    return _read_json(TOKEN_FILE, None)

def save_token(token: dict) -> None:
    _write_json(TOKEN_FILE, token)

def load_runs() -> list[dict]:
    return _read_json(RUNS_FILE, [])

def save_runs(runs: list[dict]) -> None:
    _write_json(RUNS_FILE, runs)

def load_deleted_ids() -> list[str]:
    return _read_json(DELETED_IDS_FILE, [])

def _migrate_polar_runs() -> None:
    """One-time migration: polar_runs.json → all_runs.json if needed."""
    old = DATA_DIR / "polar_runs.json"
    if old.exists() and not RUNS_FILE.exists():
        old_data = _read_json(old, [])
        if old_data:
            log.info("Migrating %d runs from polar_runs.json to all_runs.json", len(old_data))
            _write_json(RUNS_FILE, old_data)

# ---------------------------------------------------------------------------
# Polar API helpers
# ---------------------------------------------------------------------------

ISO8601_DURATION_RE = re.compile(
    r"^PT"
    r"(?:(?P<h>\d+)H)?"
    r"(?:(?P<m>\d+)M)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)S)?$"
)

def iso_duration_to_seconds(s: str | None) -> float:
    if not s:
        return 0.0
    m = ISO8601_DURATION_RE.match(s)
    if not m:
        return 0.0
    h = float(m.group("h") or 0)
    mn = float(m.group("m") or 0)
    sec = float(m.group("s") or 0)
    return h * 3600 + mn * 60 + sec

def classify_run_type(run: dict, hf_max: int = 182) -> str:
    note = (run.get("note") or "").lower()
    if re.search(r"staffel|relay|rennen|wettkampf|race", note):
        return "Rennen"
    if re.search(r"tempo|threshold|schwelle|interval", note):
        return "Tempo"
    if re.search(r"long ?run|langlauf|\blr\b", note):
        return "LongRun"

    km = float(run.get("km") or 0)
    pace = float(run.get("paceSecPerKm") or 0)
    avg = float(run.get("avgHr") or 0)
    mx = float(run.get("maxHr") or 0)
    hf = float(hf_max or 182)
    avg_pct = avg / hf if avg > 0 else 0
    max_pct = mx / hf if mx > 0 else 0
    has_hr = avg > 0 or mx > 0

    if pace > 0 and pace < 330 and km >= 4 and (avg_pct >= 0.88 or max_pct >= 0.95):
        return "Rennen"
    if 310 <= pace <= 380 and km < 14:
        if (avg_pct >= 0.80) if has_hr else (pace < 360):
            return "Tempo"
    if km >= 14:
        return "LongRun"
    return "Locker"

def map_polar_to_run(ex: dict) -> dict | None:
    """Polar Exercise summary → HM-App run schema."""
    sport = (ex.get("sport") or "").upper()
    detailed = (ex.get("detailed-sport-info") or ex.get("detailedSportInfo") or "").upper()
    is_running = sport == "RUNNING" or (sport == "" and "RUNNING" in detailed)
    is_walking = sport == "WALKING"
    is_rowing  = sport == "ROWING" or "ROWING" in detailed
    if not (is_running or is_walking or is_rowing):
        return None

    start_time = ex.get("start-time") or ex.get("startTime") or ""
    duration_sec = iso_duration_to_seconds(ex.get("duration"))
    distance_m = float(ex.get("distance") or 0)
    km = distance_m / 1000.0

    hr = ex.get("heart-rate") or ex.get("heartRate") or {}
    avg_hr = hr.get("average")
    max_hr = hr.get("maximum")

    pace_sec_per_km = round(duration_sec / km) if km > 0.05 else None

    polar_id = str(ex.get("id") or "").strip() or None
    if not polar_id:
        return None

    note = f"Polar API · {detailed}".strip(" ·")
    if is_walking:
        run_type = "Walking"
    elif is_rowing:
        run_type = "Rudern"
    else:
        run_type = classify_run_type(
            {
                "km": round(km * 10) / 10,
                "paceSecPerKm": pace_sec_per_km,
                "avgHr": avg_hr,
                "maxHr": max_hr,
                "note": note,
            },
            HF_MAX,
        )

    return {
        "id": polar_id,
        "date": start_time[:10] if start_time else "",
        "type": run_type,
        "km": round(km * 10) / 10,
        "durationMin": round(duration_sec / 60 * 10) / 10,
        "paceSecPerKm": pace_sec_per_km,
        "avgHr": avg_hr,
        "maxHr": max_hr,
        "feeling": None,
        "note": note,
        "polarId": polar_id,
    }

async def polar_request(
    method: str,
    path: str,
    *,
    token: str,
    json_body: Any | None = None,
    headers_extra: dict | None = None,
) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if headers_extra:
        headers.update(headers_extra)
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.request(method, POLAR_API_BASE + path, headers=headers, json=json_body)

# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

async def pull_polar_exercises() -> dict:
    """
    Polar AccessLink v3 transactional pull:
      1. POST /v3/users/{user-id}/exercise-transactions     → tx_id
      2. GET  ...exercise-transactions/{tx_id}              → list of URLs
      3. GET  {url}                                          → exercise summary
      4. PUT  ...exercise-transactions/{tx_id}              → commit
    """
    tok = load_token()
    if not tok or "access_token" not in tok or "x_user_id" not in tok:
        raise HTTPException(status_code=401, detail="Not authorised. Visit /api/polar/auth/start.")

    access_token = tok["access_token"]
    user_id = tok["x_user_id"]

    # 1. Start transaction
    r = await polar_request(
        "POST",
        f"/v3/users/{user_id}/exercise-transactions",
        token=access_token,
    )
    if r.status_code == 204:
        log.info("Polar: no new exercises (204)")
        _write_last_sync(0)
        return {"new": 0, "total": len(load_runs())}
    if r.status_code != 201:
        raise HTTPException(
            status_code=502,
            detail=f"Polar tx create failed: {r.status_code} {r.text}",
        )

    tx_data = r.json()
    tx_url = tx_data.get("resource-uri") or tx_data.get("transaction-id")
    # tx_url may be relative or absolute; extract id
    tx_match = re.search(r"exercise-transactions/(\d+)", str(tx_url))
    if not tx_match:
        raise HTTPException(502, f"Cannot extract transaction id from {tx_url}")
    tx_id = tx_match.group(1)

    # 2. List exercises in this transaction
    r = await polar_request(
        "GET",
        f"/v3/users/{user_id}/exercise-transactions/{tx_id}",
        token=access_token,
    )
    if r.status_code != 200:
        raise HTTPException(502, f"Polar tx list failed: {r.status_code} {r.text}")
    listing = r.json()
    exercise_urls: list[str] = listing.get("exercises", [])

    # 3. Fetch each exercise
    new_runs: list[dict] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for url in exercise_urls:
            er = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            if er.status_code != 200:
                log.warning("Polar exercise fetch failed: %s %s", er.status_code, url)
                continue
            run = map_polar_to_run(er.json())
            if run:
                new_runs.append(run)

    # 4. Merge with existing, dedup by id/polarId, skip deleted
    existing = load_runs()
    deleted_ids = set(load_deleted_ids())
    existing_ids = {r.get("id") or r.get("polarId") for r in existing if r.get("id") or r.get("polarId")}
    added = 0
    for run in new_runs:
        run_id = run["id"]
        if run_id in deleted_ids or run_id in existing_ids:
            continue
        existing.append(run)
        existing_ids.add(run_id)
        added += 1
    save_runs(existing)

    # 5. Commit transaction (marks exercises as fetched)
    r = await polar_request(
        "PUT",
        f"/v3/users/{user_id}/exercise-transactions/{tx_id}",
        token=access_token,
    )
    if r.status_code != 200:
        log.warning("Polar tx commit returned %s — but exercises already saved locally", r.status_code)

    _write_last_sync(added)
    log.info("Polar sync: %d new exercises (total %d)", added, len(existing))
    return {"new": added, "total": len(existing)}

def _write_last_sync(added: int) -> None:
    LAST_SYNC_FILE.write_text(json.dumps({
        "at": datetime.now(timezone.utc).isoformat(),
        "added": added,
    }))

# ---------------------------------------------------------------------------
# FastAPI app + scheduler lifecycle
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler(timezone="UTC")

@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_data_dir()
    _migrate_polar_runs()
    # Daily cron at SYNC_HOUR_UTC
    scheduler.add_job(
        _scheduled_sync,
        CronTrigger(hour=SYNC_HOUR_UTC, minute=0),
        id="daily_polar_sync",
        replace_existing=True,
    )
    scheduler.start()
    log.info("Scheduler started — daily sync at %02d:00 UTC", SYNC_HOUR_UTC)
    yield
    scheduler.shutdown(wait=False)

async def _scheduled_sync() -> None:
    try:
        result = await pull_polar_exercises()
        log.info("Scheduled sync: %s", result)
    except Exception as e:
        log.exception("Scheduled sync failed: %s", e)

app = FastAPI(title="HM-App Backend", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "authorised": load_token() is not None,
        "runs_cached": len(load_runs()),
        "last_sync": _read_json(LAST_SYNC_FILE, None),
    }

@app.get("/api/runs")
async def get_runs() -> list[dict]:
    return load_runs()

@app.post("/api/runs")
async def upsert_runs(runs: list[dict] = Body(...)) -> dict:
    existing = load_runs()
    by_id: dict[str, dict] = {r["id"]: r for r in existing if r.get("id")}
    added, updated = 0, 0
    for run in runs:
        if not run.get("id"):
            continue
        if run["id"] in by_id:
            by_id[run["id"]].update(run)
            updated += 1
        else:
            existing.append(run)
            by_id[run["id"]] = run
            added += 1
    save_runs(existing)
    return {"added": added, "updated": updated}

@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str) -> dict:
    runs = [r for r in load_runs() if r.get("id") != run_id]
    save_runs(runs)
    deleted = load_deleted_ids()
    if run_id not in deleted:
        deleted.append(run_id)
    _write_json(DELETED_IDS_FILE, deleted)
    return {"ok": True}

@app.post("/api/sync-now")
async def sync_now() -> dict:
    return await pull_polar_exercises()

@app.get("/api/state")
async def get_app_state() -> dict:
    return _read_json(APP_STATE_FILE, {})

@app.post("/api/state")
async def save_app_state(payload: dict = Body(...)) -> dict:
    # Strip runs and _lastPlan from state to avoid duplication (runs have their own endpoint)
    sanitized = {k: v for k, v in payload.items() if k not in ("runs", "_lastPlan", "_backendMigrated")}
    _write_json(APP_STATE_FILE, sanitized)
    return {"ok": True}

@app.get("/api/polar/auth/start")
async def auth_start() -> RedirectResponse:
    if not POLAR_CLIENT_ID or not POLAR_REDIRECT_URI:
        raise HTTPException(500, "POLAR_CLIENT_ID / POLAR_REDIRECT_URI not configured")
    state = secrets.token_urlsafe(24)
    STATE_FILE.write_text(state)
    params = {
        "response_type": "code",
        "client_id": POLAR_CLIENT_ID,
        "redirect_uri": POLAR_REDIRECT_URI,
        "scope": "accesslink.read_all",
        "state": state,
    }
    url = f"{POLAR_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(url, status_code=302)

@app.get("/api/polar/auth/callback")
async def auth_callback(code: str | None = None, state: str | None = None, error: str | None = None) -> JSONResponse:
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    if not code:
        raise HTTPException(400, "Missing code")
    if not STATE_FILE.exists() or STATE_FILE.read_text().strip() != (state or ""):
        raise HTTPException(400, "State mismatch")

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            POLAR_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": POLAR_REDIRECT_URI,
            },
            auth=(POLAR_CLIENT_ID, POLAR_CLIENT_SECRET),
            headers={"Accept": "application/json"},
        )
    if r.status_code != 200:
        raise HTTPException(502, f"Token exchange failed: {r.status_code} {r.text}")
    token = r.json()
    # token contains: access_token, token_type=bearer, expires_in (huge), x_user_id
    save_token(token)
    STATE_FILE.unlink(missing_ok=True)

    # Register user with Polar AccessLink (one-time, idempotent)
    try:
        reg = await polar_request(
            "POST",
            "/v3/users",
            token=token["access_token"],
            json_body={"member-id": str(token.get("x_user_id"))},
        )
        if reg.status_code not in (200, 201, 409):  # 409 = already registered
            log.warning("User registration unexpected status: %s %s", reg.status_code, reg.text)
    except Exception as e:
        log.warning("User registration failed (non-fatal): %s", e)

    return JSONResponse({
        "ok": True,
        "message": "Authorisierung erfolgreich. Du kannst dieses Tab schliessen und in der HM-App auf 'Sync jetzt' klicken.",
        "x_user_id": token.get("x_user_id"),
    })

# Health-check on root for sanity
@app.get("/")
async def root() -> dict:
    return {"service": "hm-app-backend", "ok": True}
