import os
import httpx
import logging
from typing import List, Dict, Any, Optional

from src.services import cache_service

logger = logging.getLogger(__name__)

IELTS_API_BASE_URL = "https://ieltsapi.mastereducation.kz/api/lms"
IELTS_API_KEY = os.getenv("IELTS_API_KEY", "")

# Results only change when students finish tests, so a short cache keeps
# leaderboard reloads from hammering the IELTS platform.
_CACHE_TTL_SECONDS = 30 * 60


class IELTSService:
    @staticmethod
    async def _post(path: str, payload: Dict[str, Any], timeout: float = 20.0) -> Optional[Dict[str, Any]]:
        """Returns the JSON body on 200, {} on 404 (no weekly set covers the
        date — a legitimate empty answer), and None on any error."""
        url = f"{IELTS_API_BASE_URL}{path}"
        headers = {
            "X-API-Key": IELTS_API_KEY,
            "Content-Type": "application/json"
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers=headers, json=payload, timeout=timeout)
                if response.status_code == 200:
                    return response.json()
                if response.status_code == 404:
                    logger.debug(f"IELTS API no data ({path}): {response.text}")
                    return {}
                logger.error(f"IELTS API error ({path}): {response.status_code} - {response.text}")
                return None
        except Exception as e:
            logger.error(f"IELTS API exception ({path}): {e}")
            return None

    @staticmethod
    async def fetch_batch_scores_by_date(emails: List[str], date_str: str) -> Dict[str, Any]:
        """
        Fetch IELTS band scores + examiner feedback for a batch of students,
        for the weekly set that covers date_str (ISO YYYY-MM-DD).
          POST /api/lms/students/batch-scores-by-date
        Max 500 emails per call; chunked here.
        """
        if not emails:
            return {"results": []}

        cache_key = f"ielts:batch-scores:{date_str}:{cache_service._stable_hash(sorted(emails))}"
        cached = cache_service.get_json(cache_key)
        if cached is not None:
            return cached

        merged: Dict[str, Any] = {}
        all_results: List[Dict[str, Any]] = []
        had_error = False
        for i in range(0, len(emails), 500):
            chunk = emails[i:i + 500]
            payload = {
                "date": date_str,
                "emails": chunk,
            }
            data = await IELTSService._post("/students/batch-scores-by-date", payload)
            if data is None:
                had_error = True
                continue
            if data and not merged:
                merged = {k: v for k, v in data.items() if k != "results"}
            all_results.extend(data.get("results", []))

        merged["results"] = all_results
        # Cache real answers (including "no test that week"), but never a
        # transient API failure.
        if not had_error:
            cache_service.set_json(cache_key, merged, ttl_seconds=_CACHE_TTL_SECONDS)
        return merged
