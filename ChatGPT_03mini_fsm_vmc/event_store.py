import json
from datetime import datetime, timezone
from loguru import logger
from pathlib import Path
from typing import List, Type, TypeVar, Optional
from pydantic import BaseModel, Field

# Generic type for events
E = TypeVar('E', bound='EventBase')

class EventBase(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    type: str

    # Centralize ISO-format JSON encoding for all datetime fields
    model_config = {
        "json_encoders": {
            datetime: lambda dt: dt.isoformat()
        }
    }

class TransactionEvent(EventBase):
    type: str = Field(default='transaction')
    channel: str
    sku: str
    amount: float
    fsm_state_before: Optional[str]
    fsm_state_after: Optional[str]

class SnapshotEvent(EventBase):
    type: str = Field(default='snapshot')
    # embed full state snapshot as JSON string (or dict)
    state: dict

class EventStore:
    def __init__(self, log_path: Path, snapshot_path: Path, snapshot_every: int = 100):
        self.log_path = log_path
        self.snapshot_path = snapshot_path
        self.snapshot_every = snapshot_every
        self._events_since_snapshot = 0

        # Ensure files exist
        self.log_path.touch(exist_ok=True)
        self.snapshot_path.touch(exist_ok=True)

    def append_event(self, event: EventBase) -> None:
        """
        Append a single event as a JSON line to the log file.
        """
        with self.log_path.open('a', encoding='utf-8') as f:
            # Use Pydantic’s built-in JSON serializer
            f.write(event.model_dump_json() + "\n")
        self._events_since_snapshot += 1

    def replay_events(self, event_type: Type[E]) -> List[E]:
        """
        Read the log file and deserialize events of the given type.
        """
        events: List[E] = []
        with self.log_path.open('r', encoding='utf-8') as f:
            for line in f:
                try:
                    payload = json.loads(line)
                    if payload.get('type') == event_type.__fields__['type'].default:
                        events.append(event_type.parse_obj(payload))
                except json.JSONDecodeError as err:
                    logger.warning(f"Skipping malformed event line: {err}")
                    continue
        return events

    def load_latest_snapshot(self) -> Optional[SnapshotEvent]:
        """
        Load the last snapshot event from the snapshot file.
        """
        with self.snapshot_path.open('r', encoding='utf-8') as f:
            text = f.read().strip()
            if not text:
                return None
            return SnapshotEvent.parse_raw(text)

    def write_snapshot(self, state: dict) -> None:
        """
        Write a snapshot event to snapshot_path and reset the log.
        """
        snapshot = SnapshotEvent(state=state)
        with self.snapshot_path.open('w', encoding='utf-8') as f:
            # Consistently use Pydantic’s JSON dumper
            f.write(snapshot.model_dump_json(indent=2))
        # Clear the log
        self.log_path.write_text('')
        self._events_since_snapshot = 0

    def checkpoint(self, state: dict) -> None:
        """
        If enough events have occurred, create a new snapshot.
        """
        if self._events_since_snapshot >= self.snapshot_every:
            self.write_snapshot(state)
