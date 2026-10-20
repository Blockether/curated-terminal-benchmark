"""Check the live website assets used by the report presentation."""

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

BRAND = Path(__file__).resolve().parents[1] / "results/opus-comparison-branded"


def test_wordmark_preserves_live_browser_geometry():
    source = json.loads((BRAND / "brand-source.json").read_text())
    root = ElementTree.parse(BRAND / "brand-lockup.svg").getroot()
    assert float(root.attrib["width"].removesuffix("pt")) == source["geometry"]["width"]
    assert float(root.attrib["height"].removesuffix("pt")) == source["geometry"]["height"]
    paths = root.findall("{http://www.w3.org/2000/svg}path")
    assert len(paths) == len("BLOCKETHER.")
    assert paths[-1].attrib["fill"] == "#f0ad00"
    assert source["wordmark"]["letter_spacing_px"] == -0.45


def test_font_matches_the_live_website_download():
    source = json.loads((BRAND / "brand-source.json").read_text())
    font = BRAND / "fonts" / source["font_url"].rsplit("/", 1)[1]
    assert hashlib.sha256(font.read_bytes()).hexdigest() == source["font_sha256"]


def test_static_weights_have_distinct_pdf_face_names():
    # Identical names made ReportLab reuse Regular for Bold and Black.
    ttlib = pytest.importorskip("fontTools.ttLib")
    names = []
    for label, weight in (("Regular", 400), ("Bold", 700), ("Black", 900)):
        with ttlib.TTFont(BRAND / "fonts" / f"Inter-{label}.ttf") as font:
            names.append(font["name"].getDebugName(6))
            assert font["OS/2"].usWeightClass == weight
    assert names == ["Inter-Regular", "Inter-Bold", "Inter-Black"]


def test_portrait_report_has_fixed_sections_and_readable_text():
    fitz = pytest.importorskip("fitz")
    with fitz.open(BRAND / "blockether-benchmark.pdf") as document:
        assert len(document) == 11
        for page in document:
            assert page.rect.width == pytest.approx(595.276, abs=0.01)
            assert page.rect.height == pytest.approx(841.89, abs=0.01)
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        x0, y0, x1, y1 = span["bbox"]
                        assert 40 <= x0 < x1 <= page.rect.width - 40
                        assert 0 <= y0 < y1 <= page.rect.height
                        assert span["size"] >= 9
        for index, section in (
            (0, "01 / OBJECTIVE"),
            (2, "02 / RESULTS"),
            (8, "03 / INTERPRETATION"),
            (9, "04 / REFERENCES"),
            (10, "05 / FURTHER WORK"),
        ):
            assert section in document[index].get_text()
            spans = [
                span
                for block in document[index].get_text("dict")["blocks"]
                for line in block.get("lines", [])
                for span in line["spans"]
            ]
            label = next(span for span in spans if span["text"] == section)
            assert label["bbox"][0] == pytest.approx(48, abs=0.1)
            assert label["origin"][1] == pytest.approx(document[index].rect.height - 27, abs=0.1)
            date = next(span for span in spans if span["text"] == "18 October 2026")
            assert date["bbox"][2] == pytest.approx(document[index].rect.width - 48, abs=0.1)
            assert date["origin"][1] == pytest.approx(65, abs=0.1)
        assert "Task selection" in document[0].get_text()
        assert "Execution and verification" in document[1].get_text()
        assert "8/12" in document[8].get_text()


def test_metrics_report_shows_all_requested_fields():
    fitz = pytest.importorskip("fitz")
    path = BRAND.parent / "dirge-comparison/blockether-benchmark-metrics.pdf"
    with fitz.open(path) as document:
        text = "\n".join(page.get_text() for page in document)
        for label in ("Harness:", "Model:", "Input tokens:", "Output tokens:", "Time:", "Cost:"):
            assert text.count(label) == 7
        assert "Dirge: sealed results" not in text
        assert "Input includes cached tokens" in text
        for page in document:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        x0, y0, x1, y1 = span["bbox"]
                        assert 40 <= x0 < x1 <= page.rect.width - 40
                        assert 0 <= y0 < y1 <= page.rect.height
