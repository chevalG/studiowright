"""The project media list and the recovery of images whose reply was lost.

Payloads are anonymised copies of the shapes Flow sent on 2026-10-25.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from studiowright.flow import batchexecute as be
from studiowright.flow import replies as r
from studiowright.flow.page import ReplyWatch, _rpcs

PROJECT = "00000000-0000-4000-8000-000000000001"
OTHER_PROJECT = "00000000-0000-4000-8000-000000000002"
NEW_IMG = "11111111-1111-4111-8111-111111111111"
OLD_IMG = "22222222-2222-4222-8222-222222222222"
CLIP = "33333333-3333-4333-8333-333333333333"
UPLOAD = "44444444-4444-4444-8444-444444444444"
ELSEWHERE = "55555555-5555-4555-8555-555555555555"


def wf(n: int) -> str:
    return f"aaaaaaaa-0000-4000-8000-{n:012d}"


def workflow(n: int, title: str, media_id: str, created: int, generated: bool) -> list[Any]:
    generation = "0A0A0A0A-0000-4000-8000-0000000000AA" if generated else None
    info = [title, [created, 5000], None, None, media_id, generation, [created + 9, 0]]
    return [wf(n), None, None, info, PROJECT]


def media_record(n: int, media_id: str, clip: bool) -> list[Any]:
    details = [[1, 0], "prompt", None]
    if clip:
        return [media_id, PROJECT, wf(n), None, None, details, None, [None, [1280, 720]]]
    image = [None, [None, None, None, "https://lh3.example/x", 0, None, "image/jpeg"], [768, 1376]]
    return [media_id, PROJECT, wf(n), None, None, details, image]


def list_payload() -> list[Any]:
    """The list a project page loads: workflows, media records, other projects' items."""
    return [
        None,
        [
            workflow(1, "Old portrait", OLD_IMG, 1_700_000_000, True),
            workflow(2, "Harbour at dawn", CLIP, 1_700_000_500, True),
            workflow(3, "sw-0123456789.jpg", UPLOAD, 1_700_000_100, False),
            workflow(4, "Ceramic teapot on table", NEW_IMG, 1_700_001_000, True),
            [wf(9), None, None, ["Elsewhere", [1, 0], None, None, ELSEWHERE, "X"], OTHER_PROJECT],
        ],
        [
            media_record(1, OLD_IMG, clip=False),
            media_record(2, CLIP, clip=True),
            media_record(3, UPLOAD, clip=False),
            media_record(4, NEW_IMG, clip=False),
        ],
        [[ELSEWHERE, 1, "Backfilled character", [ELSEWHERE, "x_backfill", wf(9)]]],
        None,
        None,
        None,
        [None, None, [[None, 3, 2, None, "display"]], None, 1],
    ]


def framed(rpc: str, payload: Any) -> str:
    row = json.dumps([["wrb.fr", rpc, json.dumps(payload), None, None, None, "generic"]])
    return f")]}}'\n\n{len(row)}\n{row}\n"


def test_project_media_lists_newest_first_with_kinds() -> None:
    payload = be.parse_reply(framed("Zzl0ze", list_payload()), "Zzl0ze")
    items = r.project_media(payload, PROJECT)
    assert [(i.media_id, i.kind) for i in items] == [
        (NEW_IMG, "image"),
        (CLIP, "video"),
        (UPLOAD, "upload"),
        (OLD_IMG, "image"),
    ]
    assert items[0].title == "Ceramic teapot on table"
    assert items[0].created == 1_700_001_000 + 5000 / 1e9


def test_new_media_needs_both_a_new_id_and_a_new_time() -> None:
    items = r.project_media(list_payload(), PROJECT)
    # The grid showed only the newest tiles before the job; OLD_IMG was off screen.
    before = {CLIP, UPLOAD}
    assert [i.media_id for i in r.new_media(before, items, 1_700_000_900, 4)] == [NEW_IMG]
    # Without creation times (a grid-only list) the id diff alone decides, newest first.
    grid = [r.MediaItem(i.media_id, i.kind, i.title) for i in items]
    assert [i.media_id for i in r.new_media(before, grid, 1_700_000_900, 1)] == [NEW_IMG]
    assert r.new_media({NEW_IMG, CLIP, UPLOAD, OLD_IMG}, items, None, 4) == []
    assert [i.media_id for i in r.new_media(set(), items, None, 4, kind="video")] == [CLIP]


def test_one_request_can_carry_several_rpcs() -> None:
    base = "https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute"
    assert _rpcs(f"{base}?rpcids=ogiZ0b&source-path=%2F") == ["ogiZ0b"]
    assert _rpcs(f"{base}?rpcids=WuwhI%2CogiZ0b&rt=c") == ["WuwhI", "ogiZ0b"]
    assert _rpcs("https://flow.google.com/project/x?rpcids=ogiZ0b") == []


class FakeCdp:
    """Answers the two calls ReplyWatch makes; a cancelled request keeps no body."""

    def __init__(self, buffered: bytes, finished_early: bool = False) -> None:
        self.buffered = buffered
        self.finished_early = finished_early

    async def send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "Network.streamResourceContent":
            if self.finished_early:
                raise RuntimeError("Request with the provided ID has already finished")
            return {"bufferedData": base64.b64encode(self.buffered).decode()}
        if method == "Network.getResponseBody":
            if not self.finished_early:
                raise RuntimeError("No data found for resource with given identifier")
            return {"body": self.buffered.decode(), "base64Encoded": False}
        raise AssertionError(method)


def response_event(rpcids: str) -> dict[str, Any]:
    url = f"https://flow.google.com/_/x/data/batchexecute?rpcids={rpcids}&rt=c"
    return {"requestId": "1.1", "response": {"url": url, "status": 200}}


async def finish(watch: ReplyWatch, finished: bool) -> None:
    watch._on_end({"requestId": "1.1"}, finished)
    for task in watch._tasks:
        await task


async def test_a_cancelled_reply_is_assembled_from_its_stream() -> None:
    text = framed("ogiZ0b", [["image"]])
    watch = ReplyWatch(page=None, rpcs=["ogiZ0b"])  # type: ignore[arg-type]
    watch._cdp = FakeCdp(text[:20].encode())  # type: ignore[assignment]
    watch._on_response(response_event("WuwhI%2CogiZ0b"))
    watch._on_data({"requestId": "1.1", "data": base64.b64encode(text[20:].encode()).decode()})
    await finish(watch, finished=False)
    assert [(rpc, body) for rpc, _status, body in watch.bodies] == [("ogiZ0b", text)]
    assert be.parse_reply(watch.bodies[0][2], "ogiZ0b") == [["image"]]


async def test_a_reply_that_finished_early_is_read_whole() -> None:
    text = framed("ogiZ0b", [["image"]])
    watch = ReplyWatch(page=None, rpcs=None)  # type: ignore[arg-type]
    watch._cdp = FakeCdp(text.encode(), finished_early=True)  # type: ignore[assignment]
    watch._on_response(response_event("ogiZ0b"))
    await finish(watch, finished=True)
    assert watch.bodies == [("ogiZ0b", 200, text)]


async def test_a_lost_reply_is_reported_unreadable() -> None:
    watch = ReplyWatch(page=None, rpcs=["ogiZ0b"])  # type: ignore[arg-type]
    watch._cdp = FakeCdp(b"", finished_early=True)  # type: ignore[assignment]
    watch._on_response(response_event("ogiZ0b"))
    await finish(watch, finished=False)
    assert watch.bodies == [] and watch.unreadable == ["ogiZ0b"]


async def test_unwanted_replies_are_ignored() -> None:
    watch = ReplyWatch(page=None, rpcs=["ogiZ0b"])  # type: ignore[arg-type]
    watch._cdp = FakeCdp(b"")  # type: ignore[assignment]
    watch._on_response(response_event("ngNC2"))
    assert watch._replies == {}
