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

"""Protocol validation for Context Pack inputs."""

import pytest

from powercontext.builtin.runtime.context_pack import ContextPack, ContextPackDelta, ContextPackError


@pytest.mark.parametrize("chunk_bytes", [4, 7, 10, 512])
def test_context_pack_splits_text_into_independently_valid_utf8_chunks(chunk_bytes: int) -> None:
    content = "海洋🙂\nASCII text\n水下传感器"
    pack = ContextPack.from_content(content, chunk_bytes=chunk_bytes)
    assert all(0 < len(item) <= chunk_bytes for item in pack.items)
    assert "".join(item.decode("utf-8") for item in pack.items) == content
    assert pack == ContextPack.from_content(content, chunk_bytes=chunk_bytes)


@pytest.mark.parametrize("replacement", [b"YR==", b"YQ--"])
def test_context_pack_rejects_invalid_base64(replacement: bytes) -> None:
    wire = ContextPack.build((b"a",)).to_bytes()
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(wire.replace(b"YQ==", replacement))


def test_context_pack_delta_rejects_duplicate_fields() -> None:
    base = ContextPack.build(())
    target = ContextPack.build((b"a",))
    wire = ContextPackDelta.between(base, target).to_bytes()
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(wire.replace(b'"appended":', b'"appended":[],"appended":'))


def test_context_pack_checks_received_byte_count() -> None:
    wire = ContextPack.build((b"body",)).to_bytes()
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(wire + b" ")
