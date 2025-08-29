import logging
import time
from typing import Any
from fastapi import APIRouter, HTTPException, Request, Depends
import httpx

from routes.auth.middleware import require_admin_from_state, User
from routes.system.config import get_config, save_config, DEFAULT_MEDIA_SERVER_CONFIG
from routes.utils.celery_tasks import get_all_tasks, ProgressState

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_media_server_config() -> dict[str, Any]:
    cfg = get_config()
    ms = cfg.get("mediaServers") or {}
    # Merge defaults shallow
    merged = DEFAULT_MEDIA_SERVER_CONFIG.copy()
    for k, v in ms.items():
        if isinstance(v, dict) and k in ("jellyfin", "plex"):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


def _validate_media_server_config(ms_cfg: dict) -> tuple[bool, str]:
    try:
        interval_seconds = int(ms_cfg.get("intervalSeconds", 3600))
        if interval_seconds < 60:
            return False, "intervalSeconds must be >= 60"
        # Basic URL sanity
        for server_key in ("jellyfin", "plex"):
            srv = ms_cfg.get(server_key, {}) or {}
            if srv.get("enabled"):
                url = (srv.get("url") or "").strip()
                if not (url.startswith("http://") or url.startswith("https://")):
                    return False, f"{server_key} url must start with http:// or https://"
                if not srv.get("apiKey"):
                    return False, f"{server_key} apiKey required when enabled"
        return True, ""
    except Exception as e:
        return False, f"Validation error: {e}"


@router.get("")
@router.get("/")
async def get_media_servers(current_user: User = Depends(require_admin_from_state)):
    return _get_media_server_config()


@router.post("")
@router.post("/")
@router.put("")
@router.put("/")
async def update_media_servers(
    request: Request, current_user: User = Depends(require_admin_from_state)
):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Invalid body")
        ms_cfg = body
        valid, msg = _validate_media_server_config(ms_cfg)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)
        # Persist inside main config
        full_cfg = get_config()
        full_cfg["mediaServers"] = ms_cfg
        ok, err = save_config(full_cfg)
        if not ok:
            raise HTTPException(status_code=500, detail=err)
        return _get_media_server_config()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update mediaServers: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update configuration")


async def _trigger_jellyfin_scan(ms_cfg: dict) -> dict:
    jelly = ms_cfg.get("jellyfin", {}) or {}
    if not jelly.get("enabled"):
        return {"skipped": True}
    # Jellyfin: POST /Library/Refresh?api_key=APIKEY
    base = jelly.get("url").rstrip("/")
    url = f"{base}/Library/Refresh?api_key={jelly.get('apiKey')}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url)
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Jellyfin scan failed: {r.status_code}")
    return {"triggered": True}


async def _trigger_plex_scan(ms_cfg: dict) -> dict:
    plex = ms_cfg.get("plex", {}) or {}
    if not plex.get("enabled"):
        return {"skipped": True}
    base = plex.get("url").rstrip("/")
    token = plex.get("apiKey")
    sections_raw = plex.get("librarySectionIds") or ""
    section_ids = [s.strip() for s in sections_raw.split(",") if s.strip()]
    headers = {"X-Plex-Token": token}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        results = []
        # If specific sections provided, scan each, else scan all (trigger /library/sections/all/refresh)
        if section_ids:
            for sid in section_ids:
                url = f"{base}/library/sections/{sid}/refresh"
                r = await client.get(url)
                results.append({"section": sid, "status": r.status_code})
        else:
            url = f"{base}/library/sections/all/refresh"
            r = await client.get(url)
            results.append({"section": "all", "status": r.status_code})
    for res in results:
        if res["status"] >= 400:
            raise HTTPException(status_code=502, detail=f"Plex scan failed for section {res['section']} status {res['status']}")
    return {"triggered": True, "sections": results}


@router.post("/scan")
async def trigger_scan(current_user: User = Depends(require_admin_from_state)):
    ms_cfg = _get_media_server_config()
    try:
        jelly = await _trigger_jellyfin_scan(ms_cfg)
        plex = await _trigger_plex_scan(ms_cfg)
        return {"jellyfin": jelly, "plex": plex, "timestamp": time.time()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Media server scan error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Media server scan failed")


def queue_is_empty() -> bool:
    tasks = get_all_tasks()
    # Only consider real download tasks; ignore utility / other task types
    download_tasks = [
        t
        for t in tasks
        if t.get("download_type") in {"track", "album", "playlist"}
    ]
    if not download_tasks:
        # No active/persisted download tasks => treat as empty
        logger.debug("queue_is_empty: no download tasks present -> empty")
        return True
    terminal = {
        ProgressState.COMPLETE,
        ProgressState.DONE,
        ProgressState.ERROR,
        ProgressState.CANCELLED,
        ProgressState.SKIPPED,
        ProgressState.ERROR_RETRIED,
        ProgressState.ERROR_AUTO_CLEANED,
        # String literals (defensive) for any legacy stored values
        "done",
        "ERROR_RETRIED",
        "ERROR_AUTO_CLEANED",
        "skipped",
        "complete",
        "cancelled",
        "error",
    }
    non_terminal = [t for t in download_tasks if t.get("status") not in terminal]
    if non_terminal:
        logger.debug(
            "queue_is_empty: still non-terminal download tasks: %s",
            [
                {
                    "id": t.get("task_id"),
                    "type": t.get("download_type"),
                    "status": t.get("status"),
                }
                for t in non_terminal
            ],
        )
        return False
    logger.debug(
        "queue_is_empty: all %d download tasks terminal (%s) -> empty",
        len(download_tasks),
        {
            t.get("status")
            for t in download_tasks
        },
    )
    return True


async def trigger_scan_if_queue_empty():
    ms_cfg = _get_media_server_config()
    if not ms_cfg.get("triggerOnQueueEmpty"):
        logger.debug(
            "trigger_scan_if_queue_empty: triggerOnQueueEmpty disabled in config"
        )
        return False
    if not queue_is_empty():
        logger.debug(
            "trigger_scan_if_queue_empty: queue not empty (download tasks still active)"
        )
        return False
    try:
        await _trigger_jellyfin_scan(ms_cfg)
        await _trigger_plex_scan(ms_cfg)
        logger.info("Triggered media server scan because queue is empty")
        return True
    except Exception as e:
        logger.warning(f"Queue-empty scan trigger failed: {e}")
        return False
