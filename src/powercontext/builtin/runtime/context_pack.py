# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Chunked Context Packs and the incremental delta protocol between clients."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final

CONTEXT_PACK_SCHEMA: Final = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA: Final = "powercontext.context-pack-delta.v1"
CONTEXT_PACK_CHUNK_BYTES: Final = 512
CONTEXT_PACK_STREAM_CHUNK_BYTES: Final = 128
_DIGEST_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_PACK_FIELDS: Final = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS: Final = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})


class ContextPackError(ValueError):
    """Raised when a Context Pack or delta violates the wire contract."""


@dataclass(frozen=True, slots=True)
class ContextPack:
    """One complete chunked context payload with its integrity metadata."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        """Build the pack that carries exactly the given chunks."""

        chunks = tuple(bytes(item) for item in items)
        digest = _pack_digest(chunks)
        byte_count = _self_describing_byte_count(chunks, digest)
        return cls(items=chunks, digest=digest, byte_count=byte_count)

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = CONTEXT_PACK_CHUNK_BYTES) -> ContextPack:
        """Split one prepared context into deterministic UTF-8 safe chunks."""

        return cls.build(_chunk_content(content, chunk_bytes))

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Decode and verify one complete pack."""

        payload = _decode_object(data, "pack")
        if set(payload) != _PACK_FIELDS:
            raise ContextPackError("pack payload must contain exactly the declared fields")
        if payload["schema"] != CONTEXT_PACK_SCHEMA:
            raise ContextPackError("pack payload schema is not supported")
        items = _decode_items(payload["items"], "pack")
        digest = _decode_digest(payload["digest"], "pack")
        expected = _pack_digest(items)
        if digest != expected:
            raise ContextPackError("pack digest does not match its content")
        byte_count = _decode_byte_count(payload["byte_count"], "pack")
        if byte_count != _self_describing_byte_count(items, digest):
            raise ContextPackError("pack byte count does not match its serialized size")
        return cls(items=items, digest=digest, byte_count=byte_count)

    def to_bytes(self) -> bytes:
        """Serialize the pack into its canonical wire form."""

        return _canonical_json(
            {
                "schema": CONTEXT_PACK_SCHEMA,
                "items": _encode_items(self.items),
                "digest": self.digest,
                "byte_count": self.byte_count,
            }
        )


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """One incremental update that reuses a prefix of a base pack."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Describe how the target follows the base through a shared prefix."""

        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        """Decode and verify one delta."""

        payload = _decode_object(data, "delta")
        if set(payload) != _DELTA_FIELDS:
            raise ContextPackError("delta payload must contain exactly the declared fields")
        if payload["schema"] != CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError("delta payload schema is not supported")
        base_digest = _decode_digest(payload["base_digest"], "delta")
        target_digest = _decode_digest(payload["target_digest"], "delta")
        prefix_items = _decode_prefix(payload["prefix_items"])
        appended = _decode_items(payload["appended"], "delta")
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix_items,
            appended=appended,
        )

    def to_bytes(self) -> bytes:
        """Serialize the delta into its canonical wire form."""

        return _canonical_json(
            {
                "schema": CONTEXT_PACK_DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }
        )

    @property
    def byte_count(self) -> int:
        """Return the serialized size of this delta, which is never a wire field."""

        return len(self.to_bytes())


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from one base pack and one delta."""

    if delta.base_digest != base.digest:
        raise ContextPackError("delta base digest does not match the base pack")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds the base pack")
    items = base.items[: delta.prefix_items] + tuple(delta.appended)
    target = ContextPack.build(items)
    if target.digest != delta.target_digest:
        raise ContextPackError("delta target digest does not match the rebuilt pack")
    return target


def extend_context_pack(
    base: ContextPack,
    content: bytes,
    *,
    chunk_bytes: int = CONTEXT_PACK_STREAM_CHUNK_BYTES,
) -> ContextPack:
    """Build the pack that follows one base pack while reusing its own chunks.

    The base pack keeps the chunks the receiver already holds, so the receiver
    can reuse them without knowing how the base was chunked. Only the bytes that
    the two contents no longer share are chunked again.
    """

    shared = _common_prefix_length(b"".join(base.items), content)
    offset = 0
    reused = 0
    for item in base.items:
        if offset + len(item) > shared:
            break
        offset += len(item)
        reused += 1
    return ContextPack.build(base.items[:reused] + _chunk_bytes(content[offset:], chunk_bytes))


def _common_prefix_length(left: bytes, right: bytes) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _chunk_content(content: str, chunk_bytes: int) -> tuple[bytes, ...]:
    return _chunk_bytes(content.encode("utf-8"), chunk_bytes)


def _chunk_bytes(raw: bytes, chunk_bytes: int) -> tuple[bytes, ...]:
    if chunk_bytes < 1:
        raise ContextPackError("chunk_bytes must be a positive number of bytes")
    if not raw:
        return ()
    chunks: list[bytes] = []
    start = 0
    while start < len(raw):
        end = min(start + chunk_bytes, len(raw))
        while end > start:
            try:
                raw[start:end].decode("utf-8")
            except UnicodeDecodeError:
                end -= 1
            else:
                break
        else:
            raise ContextPackError("chunk size cannot hold a valid UTF-8 boundary")
        chunks.append(raw[start:end])
        start = end
    return tuple(chunks)


def _pack_digest(items: tuple[bytes, ...]) -> str:
    canonical = _canonical_json({"schema": CONTEXT_PACK_SCHEMA, "items": _encode_items(items)})
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _self_describing_byte_count(items: tuple[bytes, ...], digest: str) -> int:
    byte_count = 0
    for _ in range(8):
        payload = _canonical_json(
            {
                "schema": CONTEXT_PACK_SCHEMA,
                "items": _encode_items(items),
                "digest": digest,
                "byte_count": byte_count,
            }
        )
        if len(payload) == byte_count:
            return byte_count
        byte_count = len(payload)
    raise ContextPackError("pack byte count did not converge")


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: object, label: str) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError(f"{label} items must be a JSON array")
    items: list[bytes] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContextPackError(f"{label} items must be Base64 strings")
        try:
            items.append(base64.b64decode(entry, validate=True))
        except (ValueError, TypeError) as error:
            raise ContextPackError(f"{label} items must be valid Base64 strings") from error
    return tuple(items)


def _decode_object(data: bytes, label: str) -> dict[str, object]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextPackError(f"{label} payload is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ContextPackError(f"{label} payload must be a JSON object")
    return payload


def _decode_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_PATTERN.match(value):
        raise ContextPackError(f"{label} digest must be a sha256 value")
    return value


def _decode_byte_count(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ContextPackError(f"{label} byte count must be a non-negative integer")
    return value


def _decode_prefix(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ContextPackError("delta prefix must be a non-negative integer")
    return value


__all__ = [
    "CONTEXT_PACK_CHUNK_BYTES",
    "CONTEXT_PACK_DELTA_SCHEMA",
    "CONTEXT_PACK_SCHEMA",
    "CONTEXT_PACK_STREAM_CHUNK_BYTES",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
    "extend_context_pack",
]
