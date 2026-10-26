from __future__ import annotations

from studiowright.doctor import FAIL, OK, WARN, Doctor, missing_labels
from studiowright.flow.options import ImageModel


async def test_checks_record_ok_warn_and_fail() -> None:
    doctor = Doctor()

    async def fine() -> str:
        return "all there"

    async def partly() -> tuple[str, str]:
        return WARN, "one model missing"

    async def broken() -> str:
        raise RuntimeError("the generate button is missing\nlong Playwright log")

    assert await doctor.run("a", fine)
    assert await doctor.run("b", partly)
    assert not doctor.failed
    assert not await doctor.run("c", broken)
    assert [(c.name, c.status, c.detail) for c in doctor.checks] == [
        ("a", OK, "all there"),
        ("b", WARN, "one model missing"),
        ("c", FAIL, "the generate button is missing"),
    ]
    assert doctor.failed
    assert doctor.screenshots == []  # no page open yet, so nothing to photograph


def test_missing_labels_match_the_end_of_menu_entries() -> None:
    wanted = [m.label for m in ImageModel]
    offered = ["🍌 Nano Banana 2.1", "🍌 Nano Banana Pro", "Imagen 4"]
    assert missing_labels(offered, wanted) == ["Nano Banana 2 Lite"]
    # "Nano Banana 2" must not count as offered because "Nano Banana 2.1" is.
    assert missing_labels(["Nano Banana 2.1"], ["Nano Banana 2"]) == ["Nano Banana 2"]
