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
    async def fetch_batch_latest_test_details(emails: List[str]) -> Dict[str, Any]:
        """Fetch latest test details for a batch of student emails"""
        url = f"{SAT_API_BASE_URL}/students/latest-test-details"
        headers = {
            "X-API-Key": SAT_API_KEY,
            "Content-Type": "application/json"
        }
        payload = {
            "emails": emails,
            "limit": 100
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers=headers, json=payload, timeout=15.0)
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"SAT Batch API error: {response.status_code} - {response.text}")
                    return {"results": []}
        except Exception as e:
            logger.error(f"SAT Batch API exception: {e}")
            return {"results": []}

    @staticmethod
    async def fetch_batch_test_results(emails: List[str]) -> Dict[str, Any]:
        """Fetch all test results for a batch of student emails with chunking (max 50)"""
        if not emails:
            return {"results": []}
            
        url = f"{SAT_API_BASE_URL}/students/test-results"
        headers = {
            "X-API-Key": SAT_API_KEY,
            "Content-Type": "application/json"
        }
        
        all_results = []
        # Chunk emails into 50 (based on documentation limit)
        for i in range(0, len(emails), 50):
            chunk = emails[i:i + 50]
            payload = {
                "emails": chunk,
                "limit": 50
            }
            
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, headers=headers, json=payload, timeout=20.0)
                    if response.status_code == 200:
                        data = response.json()
                        all_results.extend(data.get("results", []))
                    else:
                        logger.error(f"SAT Batch API error (chunk {i}): {response.status_code} - {response.text}")
            except Exception as e:
                logger.error(f"SAT Batch API exception (chunk {i}): {e}")
        
        return {"results": all_results}

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
