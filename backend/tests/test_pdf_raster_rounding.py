"""Renderer integer dimensions must fit the existing raster safety limits."""

import math
import sys
from types import SimpleNamespace

import pytest

from app.services import capture_reading, pdf_ocr
from tests.test_capture_reading_facts import controlled_engine as controlled_engine


@pytest.mark.parametrize(
    "width,height,full_dpi",
    [
        (1400.0, 2000.0, False),
        (7200.0, 7200.0, False),
        (612.0, 792.0, True),
        (1.25, 1_000_000.0, False),
    ],
)
def test_ceil_rounded_render_stays_inside_existing_raster_limits(
    controlled_engine, monkeypatch, width, height, full_dpi
):
    rendered, closed = [], []

    class Image:
        def __init__(self, scale):
            self.width, self.height = math.ceil(width * scale), math.ceil(height * scale)
            self.size = (self.width, self.height)

        def close(self):
            closed.append("image")

    class Bitmap:
        def __init__(self, scale):
            self.image = Image(scale)

        def to_pil(self):
            return self.image

        def close(self):
            closed.append("bitmap")

    class Page:
        def get_width(self):
            return width

        def get_height(self):
            return height

        def render(self, *, scale):
            rendered.append(scale)
            return Bitmap(scale)

        def close(self):
            closed.append("page")

    class Document:
        def __init__(self, content):
            pass

        def __len__(self):
            return 1

        def __getitem__(self, index):
            assert index == 0
            return Page()

        def close(self):
            closed.append("document")

    monkeypatch.setitem(sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=Document))
    with capture_reading.collect() as observed:
        selected = pdf_ocr._ocr_pages(b"synthetic integer-rounded renderer")
    fact = observed.page_facts().pages[0]
    assert selected[0] == "Scanned service 1 20.00 20.00"
    assert fact.status == "ocr_complete" and fact.render_dpi == 300
    assert fact.render_scale == rendered[0]
    assert fact.raster_width == math.ceil(width * rendered[0])
    assert fact.raster_height == math.ceil(height * rendered[0])
    assert fact.raster_width * fact.raster_height <= 40_000_000
    assert max(fact.raster_width, fact.raster_height) <= 20_000
    assert rendered[0] <= 300 / 72
    if full_dpi:
        assert rendered == [300 / 72]
    assert closed == ["image", "bitmap", "page", "document"]
