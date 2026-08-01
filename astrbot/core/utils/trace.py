import time
import uuid
from typing import Any


class TraceSpan:
    def __init__(
        self,
        name: str,
        umo: str | None = None,
        sender_name: str | None = None,
        message_outline: str | None = None,
    ) -> None:
        self.span_id = str(uuid.uuid4())
        self.name = name
        self.umo = umo
        self.sender_name = sender_name
        self.message_outline = message_outline
        self.started_at = time.time()

    def record(self, action: str, **fields: Any) -> None:
        """Preserve the old public call shape without producing trace output."""

        return None
