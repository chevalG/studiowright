"""Run a list of image, video and music jobs in order, resumably.

A batch file is JSON Lines (one job per line) or a JSON list. Each job is an object:

    {"id": "shot-01", "kind": "image", "prompt": "...", "model": "nano-banana-2.1",
     "aspect": "16:9", "count": 1, "refs": ["a.png"], "size": "2K"}
    {"id": "shot-01-clip", "kind": "video", "prompt": "...", "start": "shot-01",
     "resolution": "720p", "duration": 8, "size": "1080p"}
    {"id": "bed", "kind": "music", "sound": "calm piano underscore", "length": "1:30"}
    {"id": "bed-long", "kind": "music", "action": "extend", "song": "bed", "end": "2:30"}

``start``, ``end`` and ``refs`` accept file paths, media ids, or the ``id`` of an earlier
job in the same batch (its first result is used); so does a music job's ``song``.
Results are appended to ``<batch>.results.jsonl``; running the batch again skips jobs that
already finished.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from studiowright.flow.options import ImageOptions, OptionError, VideoOptions
from studiowright.music.options import (
    MUSIC_ACTIONS,
    AudioFormat,
    EditRequest,
    MusicJob,
    MusicRequest,
    VideoTemplate,
)
from studiowright.studio import ImageRequest, VideoRequest

IMAGE_KEYS = {"id", "kind", "prompt", "model", "aspect", "count", "refs", "project", "size", "out"}
MUSIC_KEYS = {
    "id",
    "kind",
    "action",
    "sound",
    "song",
    "prompt",
    "lyrics",
    "bpm",
    "length",
    "start",
    "end",
    "seed",
    "model",
    "title",
    "cover",
    "cover_prompt",
    "template",
    "format",
    "out",
}
VIDEO_KEYS = IMAGE_KEYS | {"mode", "start", "end", "resolution", "duration", "max_credits"}


class BatchError(ValueError):
    """The batch file is not valid."""


def load_jobs(path: Path) -> list[dict[str, Any]]:
    # utf-8-sig: Windows tools often start the file with a byte-order mark.
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    if text.startswith("["):
        jobs = json.loads(text)
    else:
        jobs = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not isinstance(jobs, list) or not all(isinstance(j, dict) for j in jobs):
        raise BatchError("a batch is a JSON list of objects or one JSON object per line")
    for index, job in enumerate(jobs):
        job.setdefault("id", f"job-{index + 1:03d}")
        kind = job.get("kind")
        if kind not in ("image", "video", "music"):
            raise BatchError(f"{job['id']}: kind must be 'image', 'video' or 'music'")
        if kind == "music":
            action = job.get("action", "create")
            if action not in MUSIC_ACTIONS:
                raise BatchError(f"{job['id']}: action must be one of {', '.join(MUSIC_ACTIONS)}")
            needed = "sound" if action == "create" else "song"
            if not job.get(needed):
                raise BatchError(f"{job['id']}: {needed} is required for music {action}")
        elif not job.get("prompt"):
            raise BatchError(f"{job['id']}: prompt is required")
        keys = {"image": IMAGE_KEYS, "video": VIDEO_KEYS, "music": MUSIC_KEYS}[kind]
        unknown = set(job) - keys
        if unknown:
            raise BatchError(f"{job['id']}: unknown fields {sorted(unknown)}")
    ids = [job["id"] for job in jobs]
    if len(ids) != len(set(ids)):
        raise BatchError("job ids must be unique")
    return jobs


def results_path(batch: Path) -> Path:
    return batch.with_name(batch.name + ".results.jsonl")


def finished(results: Path) -> dict[str, dict[str, Any]]:
    """Jobs already done in earlier runs, by id."""
    done: dict[str, dict[str, Any]] = {}
    if results.exists():
        for line in results.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("status") == "done":
                    done[record["id"]] = record
    return done


def _resolve(value: str | None, done: dict[str, dict[str, Any]]) -> str | None:
    """A reference to an earlier job's id becomes that job's first media or song id."""
    if value and value in done:
        result = done[value].get("result", {})
        media = result.get("media_ids") or [song["id"] for song in result.get("songs") or []]
        if media:
            return media[0]
    return value


def build_request(
    job: dict[str, Any], done: dict[str, dict[str, Any]], base: Path
) -> ImageRequest | VideoRequest | MusicJob:
    out = Path(job["out"]) if job.get("out") else None
    if out is not None and not out.is_absolute():
        out = base / out
    if job["kind"] == "music":
        return _music_job(job, done, base, out)
    refs = tuple(_resolve(r, done) or r for r in job.get("refs", []))
    refs = tuple(
        str(base / r) if not Path(r).is_absolute() and (base / r).is_file() else r for r in refs
    )
    if job["kind"] == "image":
        options = ImageOptions.build(
            job.get("model", ImageOptions().model.value),
            job.get("aspect", "16:9"),
            int(job.get("count", 1)),
        )
        return ImageRequest(job["prompt"], options, refs, job.get("project"), out, job.get("size"))
    options = VideoOptions.build(
        job.get("model", VideoOptions().model.value),
        job.get("mode", "frames"),
        job.get("aspect", "16:9"),
        job.get("resolution", "720p"),
        int(job.get("duration", 8)),
        int(job.get("count", 1)),
    )

    def frame(value: str | None) -> str | None:
        value = _resolve(value, done)
        if value and not Path(value).is_absolute() and (base / value).is_file():
            return str(base / value)
        return value

    return VideoRequest(
        job["prompt"],
        options,
        frame(job.get("start")),
        frame(job.get("end")),
        refs,
        job.get("project"),
        out,
        job.get("size"),
        job.get("max_credits"),
    )


def _music_job(
    job: dict[str, Any], done: dict[str, dict[str, Any]], base: Path, out: Path | None
) -> MusicJob:
    action = job.get("action", "create")
    audio_format = job.get("format", "wav")
    cover = job.get("cover")
    if cover and not Path(cover).is_absolute():
        cover = str(base / cover)
    if action == "create":
        request = MusicRequest.build(
            job["sound"],
            lyrics=job.get("lyrics"),
            bpm=job.get("bpm"),
            length=job.get("length"),
            seed=job.get("seed"),
            model=job.get("model", "lyria-3.5"),
            title=job.get("title"),
            audio_format=audio_format,
            out_dir=out,
            cover_image=cover,
            cover_prompt=job.get("cover_prompt"),
        )
        return MusicJob(action, request, out_dir=out)
    song = _resolve(job["song"], done) or job["song"]
    edit = None
    if action in ("extend", "cover", "replace"):
        edit = EditRequest.build(
            song,
            action,
            prompt=job.get("prompt"),
            lyrics=job.get("lyrics"),
            start=job.get("start"),
            end=job.get("end"),
            seed=job.get("seed"),
            title=job.get("title"),
            audio_format=audio_format,
            out_dir=out,
            cover_image=cover,
            cover_prompt=job.get("cover_prompt"),
        )
    return MusicJob(
        action,
        edit,
        song=song,
        start=job.get("start"),
        end=job.get("end"),
        template=VideoTemplate(job.get("template", "simple")),
        audio_format=AudioFormat(audio_format),
        out_dir=out,
    )


@dataclass
class BatchSummary:
    total: int
    skipped: int = 0
    done: int = 0
    failed: int = 0


async def run_batch(
    batch: Path,
    run_image: Callable[[ImageRequest], Awaitable[dict[str, Any]]],
    run_video: Callable[[VideoRequest], Awaitable[dict[str, Any]]],
    *,
    run_music: Callable[[MusicJob], Awaitable[dict[str, Any]]] | None = None,
    keep_going: bool = False,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> BatchSummary:
    jobs = load_jobs(batch)
    results = results_path(batch)
    done = finished(results)
    summary = BatchSummary(total=len(jobs))
    for job in jobs:
        if job["id"] in done:
            summary.skipped += 1
            continue
        started = time.time()
        record: dict[str, Any] = {"id": job["id"], "kind": job["kind"], "started": started}
        try:
            request = build_request(job, done, batch.parent)
            if isinstance(request, MusicJob):
                if run_music is None:
                    raise BatchError("this runner cannot make music")
                result = await run_music(request)
            elif isinstance(request, ImageRequest):
                result = await run_image(request)
            else:
                result = await run_video(request)
            record.update(status="done", result=result)
            done[job["id"]] = record
            summary.done += 1
        except (OptionError, BatchError) as exc:
            record.update(status="invalid", error=str(exc))
            summary.failed += 1
        except Exception as exc:
            record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            summary.failed += 1
        record["seconds"] = round(time.time() - started, 1)
        with results.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if on_event:
            on_event(record)
        if record["status"] != "done" and not keep_going:
            break
    return summary
