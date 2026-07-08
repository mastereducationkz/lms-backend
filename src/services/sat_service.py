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

    @staticmethod
    async def fetch_batch_test_results(emails: List[str]) -> Dict[str, Any]:
        """Fetch all test results for a batch of student emails with chunking (max 50)"""
        if not emails:
            return {"results": []}
            
        all_results = []
        # Chunk emails into 50 (based on documentation limit)
        for i in range(0, len(emails), 50):
            chunk = emails[i:i + 50]
            payload = {
                "emails": chunk,
                "limit": 50
            }
            
            data = await SATService._post("/students/test-results", payload, timeout=20.0)
            all_results.extend(data.get("results", []))
        
        return {"results": all_results}

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
    def extract_section_scores(item: Dict[str, Any]) -> Dict[str, Optional[int]]:
        """
        Normalize SAT section scores from different API response shapes.
        Falls back to known fixed question counts: Math=22, Verbal=27.
        """
        math_total = (
            item.get("mathTotalCount")
            or item.get("mathQuestionCount")
            or SATService.SAT_MATH_TOTAL_DEFAULT
        )
        verbal_total = (
            item.get("verbalTotalCount")
            or item.get("verbalQuestionCount")
            or SATService.SAT_VERBAL_TOTAL_DEFAULT
        )
        return {
            "math_correct": item.get("mathCorrectCount"),
            "verbal_correct": item.get("verbalCorrectCount"),
            "math_total": math_total,
            "verbal_total": verbal_total,
        }

    @staticmethod
    def extract_weekly_set(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Normalize the `weeklySet` object returned by the scores-by-date endpoints
        into snake_case. Returns None when no weekly set matches the date.
        Scaled scores are null when the student hasn't taken that section.
        """
        ws = item.get("weeklySet")
        if not ws:
            return None
        return {
            "id": ws.get("id"),
            "name": ws.get("name"),
            "week_number": ws.get("weekNumber"),
            "exam_type": ws.get("examType"),
            "verbal_scaled": ws.get("verbalScaled"),
            "math_scaled": ws.get("mathScaled"),
            "total": ws.get("total"),
            "completed": ws.get("completed"),
            "completed_at": ws.get("completedAt"),
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
