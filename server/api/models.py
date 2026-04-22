from pydantic import BaseModel
from typing import Optional
from uuid import UUID


class EventIn(BaseModel):
    event_id: UUID
    session_id: UUID
    machine_id: str
    user_id: str
    timestamp_utc: int
    synced_ts: int
    drift_ms: int = 0
    drift_rate_ppm: float = 0.0
    sequence_idx: int
    layer: str
    event_type: str
    process_name: Optional[str] = None
    app_version: Optional[str] = None
    window_title: Optional[str] = None
    window_class: Optional[str] = None
    element_type: Optional[str] = None
    element_name: Optional[str] = None
    element_auto_id: Optional[str] = None
    case_id: Optional[str] = None
    screenshot_path: Optional[str] = None
    screenshot_dhash: Optional[int] = None
    capture_reason: Optional[str] = None
    log_source: Optional[str] = None
    log_level: Optional[str] = None
    raw_message: Optional[str] = None
    message_hash: Optional[str] = None
    document_path: Optional[str] = None
    document_name: Optional[str] = None
    payload: dict


class EventsBatch(BaseModel):
    client_ts: int
    events: list[EventIn]


class ErrorIn(BaseModel):
    machine_id: Optional[str] = None
    stage: str
    error: str
    os_version: Optional[str] = None
    agent_version: Optional[str] = None
    ts: str
    payload: Optional[dict] = None


class HeartbeatIn(BaseModel):
    client_ts: int
    machine_id: str
    user_id: str
    session_id: UUID
    drift_ms: int
    drift_rate_ppm: float
    ntp_server_used: Optional[str] = None
    ntp_round_trip_ms: Optional[int] = None
    events_buffered: int
    sync_lag_sec: int
    agent_version: Optional[str] = None


class CommandAck(BaseModel):
    command_id: UUID
    status: str
    message: Optional[str] = None
    service_state: Optional[str] = None
    events_buffered: Optional[int] = None
    drift_ms: Optional[int] = None
