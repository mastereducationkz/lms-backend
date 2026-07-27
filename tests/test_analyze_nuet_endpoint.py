"""
Endpoint test for POST /courses/analyze-nuet-image.

Only the file-type validation path is exercised (no live OpenAI call): a non-image,
non-PDF upload must be rejected with HTTP 400 before the parser is invoked.
The handler is called directly (no HTTP/JWT), mirroring other endpoint tests.
"""
import io

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from src.courses.routes.courses import analyze_nuet_image


class _User:
    id = 1
    role = "teacher"


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_rejects_non_image_pdf_with_400():
    upload = UploadFile(
        file=io.BytesIO(b"hello"),
        filename="notes.txt",
        headers=Headers({"content-type": "text/plain"}),
    )
    with pytest.raises(HTTPException) as exc:
        _run(analyze_nuet_image(image=upload, correct_answers=None, current_user=_User(), db=None))
    assert exc.value.status_code == 400
