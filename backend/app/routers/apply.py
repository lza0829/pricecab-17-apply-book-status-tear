from fastapi import APIRouter
from pydantic import BaseModel

from app.services import apply_service as svc

router = APIRouter(prefix="/apply", tags=["apply"])


class ClockIn(BaseModel):
    business_clock: str


@router.get("/clock")
def get_clock():
    return svc.get_clock()


@router.put("/clock")
def set_clock(body: ClockIn):
    return svc.set_clock(body.business_clock)


@router.get("/batches")
def list_batches():
    return svc.list_batches()


@router.get("/state")
def apply_state():
    """互证视图：单状态 / 批次回执 / 价签本 / 历史末行 / 总览计数是否同源。"""
    return svc.get_apply_state()


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int):
    return svc.get_batch(batch_id)


@router.post("/due")
def apply_due():
    return svc.apply_due()
