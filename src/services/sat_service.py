import os
import httpx
import logging
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

SAT_API_BASE_URL = "https://api.mastereducation.kz/api/lms"
SAT_API_KEY = os.getenv("MASTEREDU_API_KEY", "")

class SATService:
    @staticmethod
    async def _post(path: str, payload: Dict[str, Any], timeout: float = 20.0,
                    exam_type: Optional[str] = None) -> Dict[str, Any]:
        url = f"{SAT_API_BASE_URL}{path}"
        headers = {
            "X-API-Key": SAT_API_KEY,
            "Content-Type": "application/json"
        }
        # Selects which product's weekly set the endpoint reports (absent ⇒ SAT).
        if exam_type:
            headers["X-Exam-Type"] = exam_type
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers=headers, json=payload, timeout=timeout)
                if response.status_code == 200:
                    return response.json()
                if response.status_code == 404:
                    logger.debug(f"SAT API no data ({path}): {response.text}")
                    return {}
                logger.error(f"SAT API error ({path}): {response.status_code} - {response.text}")
                return {}
        except Exception as e:
            logger.error(f"SAT API exception ({path}): {e}")
            return {}

    @staticmethod
    async def fetch_batch_latest_test_details(emails: List[str]) -> Dict[str, Any]:
        """Fetch latest test details for a batch of student emails"""
        payload = {
            "emails": emails,
            "limit": 100
        }
        response = await SATService._post("/students/latest-test-details", payload, timeout=15.0)
        return response if response else {"results": []}

    # Cache the upstream SAT results so the leaderboard grid does not block on a
    # remote HTTP round-trip to api.mastereducation.kz on every render. Keyed per
    # email so the cache is reused across groups/weeks (mock scores change rarely).
    _SAT_RESULTS_TTL = int(os.getenv("SAT_RESULTS_CACHE_TTL", "600"))  # 10 min
    _SAT_RESULTS_MISS_TTL = int(os.getenv("SAT_RESULTS_MISS_CACHE_TTL", "300"))  # 5 min
    _SAT_CACHE_NS = "sat:test-results"

    @staticmethod
    async def fetch_batch_test_results(emails: List[str]) -> Dict[str, Any]:
        """Fetch all test results for a batch of student emails with chunking (max 50).

        Results are cached per-email in Redis (graceful no-op if unavailable) so the
        leaderboard does not make a blocking upstream call on every request. Emails the
        upstream has no data for are cached as a short-lived negative sentinel to avoid
        re-hammering the API for students who have never taken a test.
        """
        if not emails:
            return {"results": []}

        from src.services import cache_service

        norm = [e.lower() for e in emails if e]
        cached_results: List[Dict[str, Any]] = []
        misses: List[str] = []
        for email in norm:
            hit = cache_service.get_json(f"{SATService._SAT_CACHE_NS}:{email}")
            if hit is None:
                misses.append(email)
            elif hit:  # non-empty dict => a real cached result; {} is the negative sentinel
                cached_results.append(hit)

        if misses:
            # Chunk misses into 50 (based on documentation limit)
            for i in range(0, len(misses), 50):
                chunk = misses[i:i + 50]
                payload = {"emails": chunk, "limit": 50}
                data = await SATService._post("/students/test-results", payload, timeout=20.0)
                results = data.get("results", [])

                seen = set()
                for res in results:
                    email = (res.get("email") or "").lower()
                    if email:
                        seen.add(email)
                        cache_service.set_json(
                            f"{SATService._SAT_CACHE_NS}:{email}", res,
                            ttl_seconds=SATService._SAT_RESULTS_TTL,
                        )
                    cached_results.append(res)
                # Cache negatives for emails the upstream returned nothing for.
                for email in chunk:
                    if email not in seen:
                        cache_service.set_json(
                            f"{SATService._SAT_CACHE_NS}:{email}", {},
                            ttl_seconds=SATService._SAT_RESULTS_MISS_TTL,
                        )

        return {"results": cached_results}

    @staticmethod
    async def fetch_batch_scores_by_date(emails: List[str], date_str: str,
                                         exam_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetch section scores by date for a batch of students.
        Expected endpoint:
          POST /api/lms/students/batch-scores-by-date
        exam_type selects the weekly-set product ("NUET" for NUET; None/"SAT" ⇒ SAT).
        """
        if not emails:
            return {"results": []}

        payload = {
            "emails": emails,
            "date": date_str,
        }
        data = await SATService._post("/students/batch-scores-by-date", payload, timeout=20.0,
                                      exam_type=exam_type)
        return data if data else {"results": []}

    @staticmethod
    async def fetch_batch_scores_by_week(emails: List[str], week: int,
                                         exam_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetch weekly-set results for a batch of students by course week number.
        Expected endpoint:
          POST /api/lms/students/batch-scores-by-week
        For products whose tests are named by week, not date (NUET). exam_type
        selects the product ("NUET"; None/"SAT" ⇒ SAT).
        """
        if not emails or not week:
            return {"results": []}

        payload = {
            "emails": emails,
            "week": week,
        }
        data = await SATService._post("/students/batch-scores-by-week", payload, timeout=20.0,
                                      exam_type=exam_type)
        return data if data else {"results": []}

    @staticmethod
    async def fetch_scores_by_date(email: str, date_str: str,
                                   exam_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetch section scores by date for one student.
        Expected endpoint:
          POST /api/lms/students/scores-by-date
        exam_type selects the weekly-set product ("NUET" for NUET; None/"SAT" ⇒ SAT).
        """
        payload = {
            "email": email,
            "date": date_str,
        }
        data = await SATService._post("/students/scores-by-date", payload, timeout=15.0,
                                      exam_type=exam_type)
        return data if data else {}

    SAT_MATH_TOTAL_DEFAULT = 22
    SAT_VERBAL_TOTAL_DEFAULT = 27

    @staticmethod
    def extract_section_scores(item: Dict[str, Any],
                               exam_type: Optional[str] = None) -> Dict[str, Optional[int]]:
        """
        Normalize SAT section scores from different API response shapes.
        Falls back to known fixed question counts: Math=22, Verbal=27. Those counts
        are SAT-specific, so any other product (NUET) reports no total at all rather
        than a wrong denominator — the UI renders the bare correct count in that case.

        NUET's batch-scores-by-week response nests scores under "weeklySet"
        (mathScaled/verbalScaled) instead of the flat mathCorrectCount/verbalCorrectCount
        that SAT's batch-scores-by-date returns — a different shape from a different
        upstream endpoint. Verified against the live NUET API (2026-08-12): reading only
        the flat keys left every NUET group's math/verbal always None ("Не сдано"), even
        for students who had completed the weekly set. The flat keys are tried first (in
        case a non-SAT product ever sends them directly) and the nested weeklySet is only
        a fallback when both are absent.
        """
        is_sat = not exam_type or exam_type.upper() == "SAT"
        math_total = (
            item.get("mathTotalCount")
            or item.get("mathQuestionCount")
            or (SATService.SAT_MATH_TOTAL_DEFAULT if is_sat else None)
        )
        verbal_total = (
            item.get("verbalTotalCount")
            or item.get("verbalQuestionCount")
            or (SATService.SAT_VERBAL_TOTAL_DEFAULT if is_sat else None)
        )
        math_correct = item.get("mathCorrectCount")
        verbal_correct = item.get("verbalCorrectCount")
        if not is_sat and math_correct is None and verbal_correct is None:
            weekly_set = item.get("weeklySet") or {}
            math_correct = weekly_set.get("mathScaled")
            verbal_correct = weekly_set.get("verbalScaled")
        return {
            "math_correct": math_correct,
            "verbal_correct": verbal_correct,
            "math_total": math_total,
            "verbal_total": verbal_total,
        }

    @staticmethod
    def get_percentage_for_week(student_data: Dict[str, Any], week_start: datetime, week_end: datetime) -> Optional[float]:
        """Calculate average percentage for tests taken within a specific week range"""
        test_pairs = student_data.get("testPairs", [])
        for pair in test_pairs:
            # Check mathTest or verbalTest date
            math_test = pair.get("mathTest")
            verbal_test = pair.get("verbalTest")
            
            test_date_str = None
            if math_test:
                test_date_str = math_test.get("completedAt")
            elif verbal_test:
                test_date_str = verbal_test.get("completedAt")
            
            if test_date_str:
                test_date = datetime.fromisoformat(test_date_str.replace("Z", "+00:00"))
                # Make sure test_date is naive if week_start/end are naive, or both aware
                if week_start.tzinfo is None and test_date.tzinfo is not None:
                    test_date = test_date.replace(tzinfo=None)
                
                if week_start <= test_date < week_end:
                    # Calculate average percentage
                    math_pct = math_test.get("percentage") if math_test else None
                    verbal_pct = verbal_test.get("percentage") if verbal_test else None
                    
                    percentages = [p for p in [math_pct, verbal_pct] if p is not None]
                    if percentages:
                        avg_pct = sum(percentages) / len(percentages)
                        return round(avg_pct, 1) # Return raw 0-100 percentage
        return None
