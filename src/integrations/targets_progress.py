"""Progress against targets (Platform Integration Pack §6.4, E5).

IELTS is read from ``platform_results`` only (kept by events + the nightly reconcile — never a
live IELTS call): the headline is the LATEST scored band per module within the last four weekly
sets ("—" when none), the secondary line the all-time best, the trend latest vs the previous
scored set, and the overall the IELTS rounding of the four latest bands when all four exist.
SAT current level is the latest completed weekly-set scaled scores from the SAT platform's
batch-scores-by-date (the same source the SAT tiles use).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from src.integrations.models import PlatformResult, PlatformWeeklySet
from src.integrations.targets import IELTS_MODULES, ielts_overall

logger = logging.getLogger(__name__)

WINDOW_SETS = 4
SAT_LOOKBACK_WEEKS = 6
_SAT_CACHE_TTL = 10 * 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


# --- helpers shared by the tile -------------------------------------------------------------

def gap(target: Optional[float], current: Optional[float]):
    """How far the current level is from the target (positive = still to gain)."""
    if target is None or current is None:
        return None
    value = target - current
    return round(value, 1) if isinstance(value, float) else value


def reached(target: Optional[float], current: Optional[float]) -> bool:
    """Same argument order as ``gap``: (target, current)."""
    return target is not None and current is not None and current >= target


# --- IELTS -----------------------------------------------------------------------------------

def window_set_ids(db: Session, platform: str = "ielts", now: Optional[datetime] = None, size: int = WINDOW_SETS) -> list[int]:
    """The ``size`` most recent weekly sets that have started, newest first."""
    now = now or _utcnow()
    rows = (
        db.query(PlatformWeeklySet.weekly_set_id)
        .filter(PlatformWeeklySet.platform == platform, PlatformWeeklySet.date_from <= now)
        .order_by(PlatformWeeklySet.date_to.desc(), PlatformWeeklySet.weekly_set_id.desc())
        .limit(size)
        .all()
    )
    return [set_id for (set_id,) in rows]


def ielts_progress(db: Session, user_id: int, now: Optional[datetime] = None) -> dict:
    now = now or _utcnow()
    window = window_set_ids(db, "ielts", now)
    rank = {set_id: index for index, set_id in enumerate(window)}   # 0 = most recent
    rows = (
        db.query(PlatformResult)
        .filter(PlatformResult.platform == "ielts", PlatformResult.user_id == user_id,
                PlatformResult.status == "scored", PlatformResult.band.isnot(None))
        .all()
    )
    rows.sort(key=lambda r: (r.scored_at or r.updated_at or datetime.min, r.id), reverse=True)

    modules: dict[str, dict] = {}
    for module in IELTS_MODULES:
        mine = [r for r in rows if r.module == module]
        best = max((r.band for r in mine), default=None)
        in_window = [r for r in mine if r.weekly_set_id in rank]
        # newest set first; within a set the newest scored row first (rows are already time-sorted)
        in_window.sort(key=lambda r: rank[r.weekly_set_id])
        latest = in_window[0] if in_window else None
        previous = next((r for r in in_window if latest is not None and r.weekly_set_id != latest.weekly_set_id), None)
        modules[module] = {
            "now": latest.band if latest else None,
            "set_id": latest.weekly_set_id if latest else None,
            "scored_at": _iso(latest.scored_at) if latest else None,
            "result_url": latest.result_url if latest else None,
            "previous": previous.band if previous else None,
            "trend": round(latest.band - previous.band, 1) if latest and previous else None,
            "best": best,
        }
    latest_bands = [modules[m]["now"] for m in IELTS_MODULES]
    bests = [modules[m]["best"] for m in IELTS_MODULES]
    return {
        "modules": modules,
        "overall_now": ielts_overall(latest_bands) if all(b is not None for b in latest_bands) else None,
        "overall_best": ielts_overall(bests) if all(b is not None for b in bests) else None,
        "overall_missing": [m for m in IELTS_MODULES if modules[m]["now"] is None],
        "window_set_ids": window,
    }


# --- SAT ---------------------------------------------------------------------------------------

def sat_current_from_payload(payload: dict, email: str) -> Optional[dict]:
    """Latest completed weekly-set scaled scores for ``email`` from a batch-scores-by-date payload."""
    email = (email or "").strip().lower()
    for item in (payload or {}).get("results") or []:
        if (item.get("email") or "").strip().lower() != email:
            continue
        ws = item.get("weeklySet") or {}
        if not ws.get("completed") or ws.get("total") is None:
            return None
        return {
            "total": ws.get("total"),
            "math": ws.get("mathScaled"),
            "verbal": ws.get("verbalScaled"),
            "week": ws.get("weekNumber"),
            "set_name": ws.get("name"),
            "completed_at": ws.get("completedAt"),
            "source": "weekly_set",
        }
    return None


def _default_sat_fetch(email: str, date_str: str) -> Optional[dict]:
    from src.services.sat_service import SATService

    return asyncio.run(SATService.fetch_batch_scores_by_date([email], date_str))


def sat_current(email: str, *, now: Optional[datetime] = None,
                fetch: Optional[Callable[[str, str], Optional[dict]]] = None) -> Optional[dict]:
    """Walk back week by week (up to SAT_LOOKBACK_WEEKS) until a completed weekly set appears.
    Cached for 10 minutes per student so the tile never hammers the SAT platform."""
    from src.services import cache_service

    key = f"targets:sat-current:{(email or '').strip().lower()}"
    cached = cache_service.get_json(key)
    if cached is not None:
        return cached or None
    fetch = fetch or _default_sat_fetch
    now = now or _utcnow()
    for weeks_back in range(SAT_LOOKBACK_WEEKS):
        date_str = (now - timedelta(weeks=weeks_back)).date().isoformat()
        try:
            payload = fetch(email, date_str) or {}
        except Exception as exc:  # noqa: BLE001 - the tile degrades to "—"
            logger.warning("targets: SAT fetch for %s failed: %s", date_str, exc)
            continue
        current = sat_current_from_payload(payload, email)
        if current:
            cache_service.set_json(key, current, ttl_seconds=_SAT_CACHE_TTL)
            return current
    cache_service.set_json(key, {}, ttl_seconds=_SAT_CACHE_TTL)
    return None


# --- assembly -----------------------------------------------------------------------------------

def student_progress(db: Session, user, targets: dict, *, now: Optional[datetime] = None,
                     sat_fetch: Optional[Callable[[str, str], Optional[dict]]] = None,
                     tracks: Optional[list[str]] = None) -> dict:
    """Per track: the targets, the current level and the gaps — the tile's whole payload."""
    from src.exams.tracks import resolve_student_tracks

    now = now or _utcnow()
    tracks = tracks if tracks is not None else resolve_student_tracks(db, user)
    out: dict[str, Any] = {"tracks": tracks, "targets": targets, "progress": {}}
    if "ielts" in tracks or "ielts" in targets:
        progress = ielts_progress(db, user.id, now)
        goal = (targets.get("ielts") or {}).get("targets") or {}
        progress["gaps"] = {
            "overall": gap(goal.get("overall"), progress["overall_now"]),
            **{m: gap(goal.get(m), progress["modules"][m]["now"]) for m in IELTS_MODULES},
        }
        progress["reached"] = reached(goal.get("overall"), progress["overall_now"])
        out["progress"]["ielts"] = progress
    if "sat" in tracks or "sat" in targets:
        current = sat_current(user.email, now=now, fetch=sat_fetch) if user.email else None
        goal = (targets.get("sat") or {}).get("targets") or {}
        out["progress"]["sat"] = {
            "current": current,
            "gaps": {k: gap(goal.get(k), (current or {}).get(k)) for k in ("total", "math", "verbal")},
            "reached": reached(goal.get("total"), (current or {}).get("total")),
        }
    if "nuet" in tracks or "nuet" in targets:
        out["progress"]["nuet"] = {"current": None, "gaps": {"total": None}, "reached": False}
    return out
