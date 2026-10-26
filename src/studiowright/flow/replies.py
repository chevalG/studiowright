"""Read Flow's own RPC replies, which StudioWright observes but never sends or alters.

Replies are matched by shape, never by position alone, and every lookup tolerates
missing pieces: Flow's payloads change without notice.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

IMAGE_URL_PREFIX = "https://flow-content.google/image/"
VIDEO_URL_PREFIX = "https://flow-content.google/video/"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_URL_UUID = re.compile(r"/(?:image|video)/([0-9a-f-]{36})")

#: Video record status codes seen on the wire.
STATUS_QUEUED = 6
STATUS_RUNNING = 2
STATUS_DONE = 3


def is_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID.match(value))


def walk(value: Any) -> Iterator[Any]:
    """Every node of a nested list payload, depth first."""
    yield value
    if isinstance(value, list):
        for item in value:
            yield from walk(item)


def strings(value: Any) -> list[str]:
    return [node for node in walk(value) if isinstance(node, str)]


def url_media_id(url: str) -> str | None:
    match = _URL_UUID.search(url)
    return match.group(1) if match else None


@dataclass(frozen=True)
class ImageResult:
    media_id: str
    url: str
    width: int | None = None
    height: int | None = None
    workflow_id: str | None = None
    #: Taken from the project's media list because Flow's own reply could not be read.
    recovered: bool = False


def credit_balance(payload: Any) -> int | None:
    """The credit balance a video submit reply reports after charging:
    ``[null, <balance>, [[media, …]], [[record]]]``. None for any other shape."""
    if (
        isinstance(payload, list)
        and len(payload) >= 3
        and payload[0] is None
        and isinstance(payload[1], int | float)
        and not isinstance(payload[1], bool)
        and isinstance(payload[2], list)
    ):
        return int(payload[1])
    return None


def workflow_ids(payload: Any) -> dict[str, str]:
    """Media id → workflow id (the id of its edit view), from image tuples
    ``[media_id, null, workflow_id, …]`` and records ``[media_id, project_id, workflow_id, …]``."""
    found: dict[str, str] = {}
    for node in walk(payload):
        if (
            isinstance(node, list)
            and len(node) >= 3
            and is_uuid(node[0])
            and is_uuid(node[2])
            and (node[1] is None or is_uuid(node[1]))
        ):
            found.setdefault(node[0], node[2])
    return found


def image_results(payload: Any) -> list[ImageResult]:
    """Generated images in an ``ogiZ0b`` reply: id, signed URL and size."""
    # An image tuple holds a details list with the signed URL and, later in the same
    # tuple, a bare [width, height] pair.
    sizes: dict[str, tuple[int, int]] = {}
    for node in walk(payload):
        if not isinstance(node, list):
            continue
        pairs = [
            child
            for child in node
            if isinstance(child, list)
            and len(child) == 2
            and all(isinstance(n, int) for n in child)
        ]
        if not pairs:
            continue
        for child in node:
            if not isinstance(child, list):
                continue
            for url in (s for s in child if isinstance(s, str)):
                media_id = url_media_id(url) if url.startswith(IMAGE_URL_PREFIX) else None
                if media_id:
                    sizes[media_id] = (pairs[-1][0], pairs[-1][1])
    workflows = workflow_ids(payload)
    results: dict[str, ImageResult] = {}
    for url in strings(payload):
        if url.startswith(IMAGE_URL_PREFIX):
            media_id = url_media_id(url)
            if media_id and media_id not in results:
                width, height = sizes.get(media_id, (None, None))
                results[media_id] = ImageResult(
                    media_id, url, width, height, workflows.get(media_id)
                )
    return list(results.values())


@dataclass(frozen=True)
class UploadResult:
    media_id: str
    name: str | None


def upload_result(payload: Any) -> UploadResult:
    """The new asset in a ``maseQ`` reply: its media id comes first."""
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        raise ValueError("unexpected maseQ reply shape")
    media_id = payload[0][0]
    if not is_uuid(media_id):
        raise ValueError("maseQ reply carries no media id")
    name = None
    with contextlib.suppress(IndexError, TypeError):
        name = payload[1][3][0]
    return UploadResult(media_id, name if isinstance(name, str) else None)


@dataclass(frozen=True)
class VideoRecord:
    workflow_id: str
    media_id: str
    status: int | None
    video_url: str | None
    poster_url: str | None


def video_records(payload: Any, project_id: str | None = None) -> list[VideoRecord]:
    """Records ``[media_id, project_id, workflow_id, …]`` in submit, poll or result replies.

    ``media_id`` is the id inside signed URLs; ``workflow_id`` is the id of the clip's edit
    route (measured 2026-10-10).
    """
    found: dict[str, VideoRecord] = {}
    for node in walk(payload):
        if not (
            isinstance(node, list)
            and len(node) >= 6
            and is_uuid(node[0])
            and is_uuid(node[1])
            and is_uuid(node[2])
        ):
            continue
        if project_id and node[1] != project_id:
            continue
        status = None
        details = node[5] if isinstance(node[5], list) else []
        if len(details) > 8 and isinstance(details[8], list) and details[8]:
            status = details[8][0] if isinstance(details[8][0], int) else None
        video_url = next((s for s in strings(node) if s.startswith(VIDEO_URL_PREFIX)), None)
        poster_url = next((s for s in strings(node) if s.startswith(IMAGE_URL_PREFIX)), None)
        found[node[0]] = VideoRecord(node[2], node[0], status, video_url, poster_url)
    return list(found.values())


def media_urls(payload: Any) -> dict[str, dict[str, str]]:
    """Every signed URL in a reply, keyed by media id: ``{"video": url, "image": url}``."""
    urls: dict[str, dict[str, str]] = {}
    for text in strings(payload):
        for prefix, kind in ((VIDEO_URL_PREFIX, "video"), (IMAGE_URL_PREFIX, "image")):
            if text.startswith(prefix):
                media_id = url_media_id(text)
                if media_id:
                    urls.setdefault(media_id, {}).setdefault(kind, text)
    return urls


@dataclass(frozen=True)
class MediaItem:
    media_id: str
    #: "image", "video" or "upload".
    kind: str
    title: str | None
    #: Seconds since the epoch, as Flow stamped it; None when only the grid was read.
    created: float | None = None


def _stamp(value: Any) -> float | None:
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(n, int) and not isinstance(n, bool) for n in value)
    ):
        return value[0] + value[1] / 1e9
    return None


def project_media(payload: Any, project_id: str) -> list[MediaItem]:
    """A project's media, newest first, from the list its page loads (``Zzl0ze``).

    Workflow records ``[workflow_id, null, null, [title, [s, ns], null, null, media_id,
    generation_id, …], project_id]`` name each medium; uploads have no generation id. Media
    records ``[media_id, project_id, workflow_id, null, null, details, image, video]`` tell
    clips (a video block) from images (measured 2026-10-25). Records of other projects in the
    same reply are skipped.
    """
    clips: set[str] = set()
    for node in walk(payload):
        if (
            isinstance(node, list)
            and len(node) > 7
            and is_uuid(node[0])
            and node[1] == project_id
            and is_uuid(node[2])
            and isinstance(node[7], list)
        ):
            clips.add(node[0])
    items: dict[str, MediaItem] = {}
    for node in walk(payload):
        if not (
            isinstance(node, list)
            and len(node) >= 5
            and is_uuid(node[0])
            and node[4] == project_id
            and isinstance(node[3], list)
            and len(node[3]) >= 6
            and is_uuid(node[3][4])
        ):
            continue
        info = node[3]
        media_id = info[4]
        kind = "upload" if info[5] is None else "video" if media_id in clips else "image"
        title = info[0] if isinstance(info[0], str) else None
        items.setdefault(media_id, MediaItem(media_id, kind, title, _stamp(info[1])))
    return sorted(items.values(), key=lambda item: item.created or 0.0, reverse=True)


def new_media(
    before: set[str],
    listed: Sequence[MediaItem],
    since: float | None,
    count: int,
    kind: str = "image",
) -> list[MediaItem]:
    """The newest ``count`` media of ``kind`` that were not on the grid before a job.

    ``listed`` is newest first. The grid shows only the tiles near the top, so a medium
    missing from ``before`` is not new by itself; when the list carries creation times, it
    must also be created after ``since``.
    """
    fresh = [
        item
        for item in listed
        if item.media_id not in before
        and item.kind == kind
        and (since is None or item.created is None or item.created >= since)
    ]
    return fresh[:count]
