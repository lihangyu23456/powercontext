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

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
    extend_context_pack,
)


def test_wire_payload_matches_an_independent_oracle() -> None:
    items = (b"alpha\n", "海洋".encode())
    unsigned = {
        "schema": "powercontext.context-pack.v1",
        "items": [base64.b64encode(item).decode("ascii") for item in items],
    }
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    pack = ContextPack.build(items)
    payload = json.loads(pack.to_bytes())
    assert payload == {
        **unsigned,
        "digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "byte_count": len(pack.to_bytes()),
    }
    assert pack.to_bytes() == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def test_chunked_content_is_utf8_safe_and_reconstructs() -> None:
    pack = ContextPack.from_content("alpha 海洋 beta", chunk_bytes=7)
    assert b"".join(pack.items).decode("utf-8") == "alpha 海洋 beta"
    assert all(len(item) <= 7 for item in pack.items)
    with pytest.raises(ContextPackError):
        ContextPack.from_content("海", chunk_bytes=2)


def test_pack_decoding_rejects_missing_extra_and_tampered_fields() -> None:
    pack = ContextPack.build((b"alpha",))
    payload = json.loads(pack.to_bytes())
    for field in payload:
        with pytest.raises(ContextPackError):
            ContextPack.from_bytes(json.dumps({key: value for key, value in payload.items() if key != field}).encode())
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({**payload, "extra": True}).encode())
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(pack.to_bytes().replace(pack.digest.encode(), b"sha256:" + b"0" * 64))


def test_delta_rebuilds_the_target_from_the_base() -> None:
    base = ContextPack.build((b"a", b"b", b"old"))
    for target_items in ((b"a", b"b", b"new", b"tail"), (b"new", b"b", b"old"), (b"a", b"new", b"old"), ()):
        target = ContextPack.build(target_items)
        delta = ContextPackDelta.between(base, target)
        rebuilt = apply_context_pack_delta(
            ContextPack.from_bytes(base.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes())
        )
        assert rebuilt.to_bytes() == target.to_bytes()
        assert delta.byte_count == len(delta.to_bytes())


def test_delta_rejects_a_wrong_base_prefix_or_target() -> None:
    base = ContextPack.build((b"one",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"one", b"two")))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(ContextPack.build((b"other",)), delta)
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, "sha256:" + "0" * 64, 0, (b"x",)))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, "sha256:" + "0" * 64, 5, ()))


@pytest.mark.parametrize("prefix", [True, 1.5, -1])
def test_delta_rejects_a_non_integer_or_negative_prefix(prefix: object) -> None:
    base = ContextPack.build((b"alpha",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"alpha", b"beta")))
    payload = json.loads(delta.to_bytes())
    payload["prefix_items"] = prefix
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def test_extend_reuses_the_base_chunks_whatever_their_size() -> None:
    base = ContextPack.build((b"alpha ", b"bravo ", b"charlie"))
    target = extend_context_pack(base, b"alpha bravo delta ")
    assert b"".join(target.items) == b"alpha bravo delta "
    assert target.items[:2] == base.items[:2]
    rebuilt = apply_context_pack_delta(base, ContextPackDelta.between(base, target))
    assert rebuilt.to_bytes() == target.to_bytes()


def test_empty_pack_round_trips() -> None:
    empty = ContextPack.build(())
    assert ContextPack.from_bytes(empty.to_bytes()) == empty
    assert empty.byte_count == len(empty.to_bytes())
