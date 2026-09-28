"""The Files API, simulated: upload once, reference by `file_id` in messages and the code-execution container.

Endpoints (the SDK's `client.files.*`; `client.beta.files.*` sends the same requests with a beta header):

    POST   /v1/files                multipart upload (field `file`) -> file metadata
    GET    /v1/files                list (page: data, has_more, first_id, last_id)
    GET    /v1/files/{id}           metadata
    GET    /v1/files/{id}/content   bytes
    DELETE /v1/files/{id}           -> {"id", "type": "file_deleted"}

Files live in memory for the process.  Text-like files (text, csv, json, markdown) can be read by
`MockRequest.documents` when a `document` block references them by `file_id`; any file can be mounted
into the sandbox with a `container_upload` block.
"""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import mimetypes
import threading
from dataclasses import dataclass, field

import httpx2

from .errors import ApiError, bad_request
from .render import next_id

TEXT_MIME_PREFIXES = ("text/", "application/json", "application/x-ndjson", "application/csv")


@dataclass
class StoredFile:
    id: str
    filename: str
    mime_type: str
    content: bytes
    created_at: str
    downloadable: bool = True

    def metadata(self) -> dict:
        return {"id": self.id, "type": "file", "filename": self.filename, "mime_type": self.mime_type,
                "size_bytes": len(self.content), "created_at": self.created_at, "downloadable": self.downloadable,
                "expires_at": None}

    @property
    def is_text(self) -> bool:
        return self.mime_type.startswith(TEXT_MIME_PREFIXES)

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


@dataclass
class FileStore:
    files: dict[str, StoredFile] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, filename: str, content: bytes, mime_type: str | None = None, *, downloadable: bool = True) -> StoredFile:
        mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        stored = StoredFile(id=next_id("file_mock_"), filename=filename, mime_type=mime, content=content,
                            created_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                            downloadable=downloadable)
        with self._lock:
            self.files[stored.id] = stored
        return stored

    def get(self, file_id: str) -> StoredFile:
        stored = self.files.get(file_id)
        if stored is None:
            raise ApiError(404, f"file not found: {file_id}", err_type="not_found_error")
        return stored

    def delete(self, file_id: str) -> None:
        with self._lock:
            if file_id not in self.files:
                raise ApiError(404, f"file not found: {file_id}", err_type="not_found_error")
            del self.files[file_id]

    def clear(self) -> None:
        with self._lock:
            self.files.clear()

    # ------------------------------------------------------------------ HTTP routing
    def route(self, request: httpx2.Request, request_id: str) -> httpx2.Response:
        path, method = request.url.path.rstrip("/"), request.method.upper()
        headers = {"request-id": request_id}
        if path == "/v1/files" and method == "POST":
            stored = self._upload(request)
            return httpx2.Response(200, json=stored.metadata(), headers=headers)
        if path == "/v1/files" and method == "GET":
            data = [f.metadata() for f in self.files.values()]
            return httpx2.Response(200, json={"data": data, "has_more": False,
                                              "first_id": data[0]["id"] if data else None,
                                              "last_id": data[-1]["id"] if data else None}, headers=headers)
        parts = path.split("/")
        if len(parts) >= 4 and parts[1] == "v1" and parts[2] == "files":
            file_id = parts[3]
            if len(parts) == 5 and parts[4] == "content" and method == "GET":
                stored = self.get(file_id)
                return httpx2.Response(200, content=stored.content,
                                       headers={**headers, "content-type": stored.mime_type,
                                                "content-disposition": f'attachment; filename="{stored.filename}"'})
            if len(parts) == 4 and method == "GET":
                return httpx2.Response(200, json=self.get(file_id).metadata(), headers=headers)
            if len(parts) == 4 and method == "DELETE":
                self.delete(file_id)
                return httpx2.Response(200, json={"id": file_id, "type": "file_deleted"}, headers=headers)
        raise ApiError(404, f"Not found: {method} {path}", err_type="not_found_error")

    def _upload(self, request: httpx2.Request) -> StoredFile:
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" not in content_type:
            raise bad_request("file: expected a multipart/form-data upload with a `file` field")
        raw = b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + request.content
        parsed = email.message_from_bytes(raw, policy=email.policy.HTTP)
        for part in parsed.iter_parts():
            if part.get_param("name", header="content-disposition") != "file":
                continue
            filename = part.get_filename() or "upload"
            payload = part.get_payload(decode=True) or b""
            if len(payload) > 500 * 1024 * 1024:
                raise ApiError(413, "file: exceeds the 500 MB maximum")
            return self.add(filename, payload, part.get_content_type())
        raise bad_request("file: Field required")


_STORE: FileStore | None = None
_STORE_LOCK = threading.Lock()


def get_file_store() -> FileStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = FileStore()
        return _STORE
