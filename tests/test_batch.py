from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from studiowright import batch as b
from studiowright.studio import ImageRequest, VideoRequest


def write(tmp_path: Path, jobs: list[dict[str, Any]]) -> Path:
    path = tmp_path / "jobs.jsonl"
    path.write_text("\n".join(json.dumps(j) for j in jobs), encoding="utf-8")
    return path


def test_load_assigns_ids_and_validates(tmp_path: Path) -> None:
    path = write(tmp_path, [{"kind": "image", "prompt": "a"}, {"kind": "video", "prompt": "b"}])
    assert [j["id"] for j in b.load_jobs(path)] == ["job-001", "job-002"]
    with pytest.raises(b.BatchError, match="kind"):
        b.load_jobs(write(tmp_path, [{"kind": "audio", "prompt": "x"}]))
    with pytest.raises(b.BatchError, match="unknown fields"):
        b.load_jobs(write(tmp_path, [{"kind": "image", "prompt": "x", "start": "a.png"}]))
    with pytest.raises(b.BatchError, match="unique"):
        b.load_jobs(write(tmp_path, [{"id": "a", "kind": "image", "prompt": "x"}] * 2))


def test_json_list_format(tmp_path: Path) -> None:
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps([{"id": "x", "kind": "image", "prompt": "p"}]), encoding="utf-8")
    assert b.load_jobs(path)[0]["id"] == "x"


async def test_run_resumes_and_chains_ids(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        [
            {"id": "still", "kind": "image", "prompt": "a keeper", "aspect": "16:9"},
            {
                "id": "clip",
                "kind": "video",
                "prompt": "push in",
                "start": "still",
                "duration": 4,
                "resolution": "360p",
            },
        ],
    )
    calls: list[Any] = []

    async def run_image(request: ImageRequest) -> dict[str, Any]:
        calls.append(request)
        return {"media_ids": ["11111111-1111-1111-1111-111111111111"], "files": ["a.jpg"]}

    async def run_video(request: VideoRequest) -> dict[str, Any]:
        calls.append(request)
        return {"media_ids": ["22222222-2222-2222-2222-222222222222"], "files": ["a.mp4"]}

    summary = await b.run_batch(path, run_image, run_video)
    assert (summary.done, summary.failed) == (2, 0)
    assert calls[1].start == "11111111-1111-1111-1111-111111111111"
    again = await b.run_batch(path, run_image, run_video)
    assert (again.skipped, again.done) == (2, 0)
    assert len(calls) == 2


async def test_stops_on_failure_unless_keep_going(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        [{"id": "a", "kind": "image", "prompt": "x"}, {"id": "b", "kind": "image", "prompt": "y"}],
    )

    async def boom(request: ImageRequest) -> dict[str, Any]:
        raise RuntimeError("flow said no")

    summary = await b.run_batch(path, boom, boom)
    assert (summary.failed, summary.done) == (1, 0)
    lines = b.results_path(path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["status"] == "failed"
    summary = await b.run_batch(path, boom, boom, keep_going=True)
    assert summary.failed == 2


async def test_invalid_options_are_reported_not_raised(tmp_path: Path) -> None:
    path = write(tmp_path, [{"id": "a", "kind": "video", "prompt": "x", "aspect": "1:1"}])

    async def never(request: Any) -> dict[str, Any]:
        raise AssertionError("must not run")

    summary = await b.run_batch(path, never, never)
    record = json.loads(b.results_path(path).read_text(encoding="utf-8"))
    assert summary.failed == 1 and record["status"] == "invalid"


def test_batch_file_with_a_bom_and_non_ascii_paths(tmp_path: Path) -> None:
    folder = tmp_path / "Örnek klasör" / "çekim"
    folder.mkdir(parents=True)
    (folder / "kare.png").write_bytes(b"x")
    path = folder / "jobs.jsonl"
    job = {"id": "ş1", "kind": "video", "prompt": "yağmur", "start": "kare.png"}
    # PowerShell's Out-File and Notepad write UTF-8 with a byte-order mark.
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(job, ensure_ascii=False).encode("utf-8"))
    jobs = b.load_jobs(path)
    assert jobs[0]["id"] == "ş1"
    request = b.build_request(jobs[0], {}, folder)
    assert isinstance(request, VideoRequest)
    assert request.start == str(folder / "kare.png")
