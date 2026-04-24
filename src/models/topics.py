from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


# ============ Topic Sources ============


class CreateTopicSourceRequest(BaseModel):
    name: str
    url: str
    type: str
    crawl_method: str = "web_fetch"
    crawl_interval_hours: int = 24


# ============ Topics ============


class CreateTopicRequest(BaseModel):
    source_id: Optional[str] = None
    title: str
    original_url: str
    source_page_url: Optional[str] = None
    summary: str
    score: float = 0
    score_controversy: float = 0
    score_novelty: float = 0
    score_helpfulness: float = 0
    hashtags: List[str] = []
    references: List[dict] = []
    questions: List[dict] = []


class BulkCreateTopicsRequest(BaseModel):
    topics: List[CreateTopicRequest]


class UpdateTopicStatusRequest(BaseModel):
    status: str


# ============ Questions & Answers ============


class CreateQuestionsRequest(BaseModel):
    questions: List[dict]


class SubmitAnswersRequest(BaseModel):
    answers: List[dict]


# ============ Blog Posts ============


# ============ Insight Exchanges ============


class SaveInsightExchangesRequest(BaseModel):
    exchanges: List[dict]


# ============ Blog Posts ============


class CreateBlogPostRequest(BaseModel):
    title: str
    content: str
