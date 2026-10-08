"""Object-storage payload backend for CT representation payloads.

To keep Postgres lean at scale, representation payloads can live outside the DB:
they are concatenated into append-only **pack shards** in a blob store, and the
catalog stores only ``(payload_uri, payload_offset, payload_len)`` per
representation. Serve time fetches the exact bytes with a single **byte-range
read** — so a 200-persona run pulls ~200 tiny ranges regardless of whether the
bank has 1k or 6M representations.

Two backends, selected by environment:
- ``LocalDirBlobStore`` (no ``SUPABASE_URL``): a directory on disk (dev/tests).
- ``SupabaseBlobStore``: a private Supabase Storage bucket (prod).

The ``db`` payload backend (payload text inline in Postgres) remains the default
and is unaffected; object mode is opt-in at ingest.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

CT_PAYLOAD_BUCKET = os.getenv("CT_PAYLOAD_BUCKET", "ct-payloads").strip() or "ct-payloads"
CT_PACK_SHARD_BYTES = int(os.getenv("CT_PACK_SHARD_BYTES", str(40 * 1024 * 1024)) or str(40 * 1024 * 1024))


def shard_key(version_id: str, index: int) -> str:
    """Deterministic blob key for a pack shard."""
    return f"{version_id}/pack-{index:05d}.bin"


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def fetch_range(self, key: str, offset: int, length: int) -> bytes: ...
    def fetch(self, key: str) -> bytes: ...


class LocalDirBlobStore:
    """Filesystem blob store (dev/tests). ``key`` is a relative path under ``root``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def fetch_range(self, key: str, offset: int, length: int) -> bytes:
        with open(self.root / key, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)

    def fetch(self, key: str) -> bytes:
        return (self.root / key).read_bytes()


_active_store: BlobStore | None = None
_store_lock = threading.Lock()


def get_active_store() -> BlobStore:
    """The library always passes a store explicitly; there is no global default."""
    if _active_store is None:
        raise RuntimeError("No payload store is set for this bank")
    return _active_store


def reset_active_store() -> None:
    """Test hook: drop the cached store so env changes take effect."""
    global _active_store
    _active_store = None


class PackBuilder:
    """Accumulate payloads into append-only shard objects in a blob store.

    ``add`` returns ``(shard_key, offset, length)`` for each payload; ``finalize``
    flushes the trailing shard. Shards roll over at ``shard_bytes``. With
    ``store=None`` offsets are computed but nothing is uploaded (rebuilds over
    sources whose shards already exist).
    """

    def __init__(self, store: BlobStore | None, version_id: str, shard_bytes: int = CT_PACK_SHARD_BYTES) -> None:
        self.store = store
        self.version_id = version_id
        self.shard_bytes = max(1, shard_bytes)
        self.shard_index = 0
        self.buf = bytearray()
        self.shard_count = 0

    def _key(self, index: int) -> str:
        return shard_key(self.version_id, index)

    def shard_key(self, index: int) -> str:
        return shard_key(self.version_id, index)

    def add(self, data: bytes) -> tuple[int, int, int]:
        if self.buf and len(self.buf) + len(data) > self.shard_bytes:
            self._flush()
        offset = len(self.buf)
        self.buf.extend(data)
        return self.shard_index, offset, len(data)

    def _flush(self) -> None:
        if not self.buf:
            return
        if self.store is not None:
            self.store.put(self._key(self.shard_index), bytes(self.buf))
        self.shard_count += 1
        self.shard_index += 1
        self.buf = bytearray()

    def finalize(self) -> int:
        self._flush()
        return self.shard_count
