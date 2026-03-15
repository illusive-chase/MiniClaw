"""PipeDriver — listener that bridges a PipeEnd to a Session."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from miniclaw.channels.pipe import PipeEnd
from miniclaw.listeners.base import Listener

if TYPE_CHECKING:
    from miniclaw.runtime import Runtime
    from miniclaw.session import Session

logger = logging.getLogger(__name__)


class PipeDriver(Listener):
    """Reads messages from a PipeEnd inbox and processes them through a Session.

    One PipeDriver per pipe end. Created by Runtime.connect_pipe().
    """

    def __init__(self, session: Session, pipe_end: PipeEnd) -> None:
        self._session = session
        self._pipe_end = pipe_end

    async def run(self, runtime: Runtime) -> None:
        """Main loop: read from pipe inbox, process through session."""
        logger.info("PipeDriver started for session %s (pipe: %s)",
                     self._session.id, self._pipe_end.name)

        while True:
            text = await self._pipe_end.listen()
            if text is None:
                logger.info("Pipe %s disconnected", self._pipe_end.name)
                break

            logger.debug("Pipe %s received: %s", self._pipe_end.name, text[:100])
            stream = self._session.process(text)
            await self._pipe_end.send_stream(stream)

    async def shutdown(self) -> None:
        self._pipe_end.disconnect()
