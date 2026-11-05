"""HTTP delivery of a caller-owned, already verified private document file."""

from __future__ import annotations

from starlette.responses import FileResponse

from app.services.documents import StoredFile


class TemporaryFileResponse(FileResponse):
    """Release staging even if a client disconnects or response sending fails."""

    def __init__(self, staged: StoredFile, **kwargs):
        super().__init__(staged.path, **kwargs)
        self.staged = staged
        # Existing one-time links deliver the whole artifact. Advertising byte
        # ranges would imply a resumable link contract that does not exist.
        del self.headers["accept-ranges"]

    async def __call__(self, scope, receive, send) -> None:
        scope = dict(scope)
        scope["headers"] = [
            (name, value)
            for name, value in scope["headers"]
            if name.lower() not in {b"range", b"if-range"}
        ]
        # A server-owned path-send can outlive this call. Read finite chunks
        # here so the finally cleanup happens after our final file access.
        scope["extensions"] = {
            name: value
            for name, value in scope.get("extensions", {}).items()
            if name != "http.response.pathsend"
        }
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.staged.cleanup()
