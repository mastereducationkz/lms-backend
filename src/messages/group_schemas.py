from pydantic import BaseModel
from typing import Optional


class PostGroupMessage(BaseModel):
    content: str = ""
    file_url: Optional[str] = None
