"""Getting decodable images out of a submitted file.

Two sources per PDF page, and both are needed:

- The *rendered page*, which catches a QR drawn as vector paths — one that is
  not an image at all and so cannot be extracted as one.
- The *embedded images* at native resolution, which catch a QR pasted in at a
  resolution higher than our render DPI, where rendering would throw away the
  detail that makes it readable.

Caps come from config so one oversized submission cannot stall a run, and the
result says which caps were hit, so a truncated scan is never silently read as
a clean one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..core.config import get
from .models import ImageSource


@dataclass
class SourceImage:
    """One image to try decoding, and where it came from."""

    array: np.ndarray
    page: int | None
    source: ImageSource
    # Identifies the image within its page, for evidence links.
    label: str


@dataclass
class ImageHarvest:
    images: list[SourceImage] = field(default_factory=list)
    pages_examined: int = 0
    images_examined: int = 0
    caps_reached: list[str] = field(default_factory=list)
    # Hosts of clickable link annotations in the PDF, for the link check.
    link_hosts: list[str] = field(default_factory=list)
    link_uris: list[str] = field(default_factory=list)


class ImageLoadError(RuntimeError):
    """The file could not be opened or read as a document or image."""


class PasswordProtectedError(ImageLoadError):
    pass


def harvest(path: Path, cfg: dict[str, Any]) -> ImageHarvest:
    """Collect every image worth decoding from the file at `path`."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _harvest_pdf(path, cfg)
    return _harvest_image_file(path, cfg)


def _harvest_image_file(path: Path, cfg: dict[str, Any]) -> ImageHarvest:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as handle:
            array = np.array(handle.convert("RGB"))
    except UnidentifiedImageError as exc:
        raise ImageLoadError(f"not a readable image: {exc}") from exc
    except OSError as exc:
        raise ImageLoadError(f"could not read image: {exc}") from exc

    return ImageHarvest(
        images=[
            SourceImage(
                array=array,
                page=1,
                source=ImageSource.IMAGE_FILE,
                label="image_file",
            )
        ],
        pages_examined=1,
        images_examined=1,
    )


def _harvest_pdf(path: Path, cfg: dict[str, Any]) -> ImageHarvest:
    import fitz

    dpi = int(get(cfg, "images.render_dpi", 300))
    max_pages = int(get(cfg, "images.max_pages", 50))
    max_per_page = int(get(cfg, "images.max_embedded_images_per_page", 20))
    max_total = int(get(cfg, "images.max_embedded_images_total", 100))
    min_pixels = int(get(cfg, "images.min_embedded_image_pixels", 2500))

    harvest_result = ImageHarvest()

    try:
        document = fitz.open(path)
    except Exception as exc:
        raise ImageLoadError(f"could not open PDF: {exc}") from exc

    with document:
        if document.needs_pass:
            raise PasswordProtectedError("PDF is password protected")

        page_count = document.page_count
        if page_count > max_pages:
            harvest_result.caps_reached.append(
                f"max_pages ({max_pages}) reached; {page_count} pages in document"
            )

        embedded_total = 0
        for index in range(min(page_count, max_pages)):
            page = document.load_page(index)
            page_number = index + 1
            harvest_result.pages_examined += 1

            rendered = _render_page(page, dpi)
            if rendered is not None:
                harvest_result.images.append(
                    SourceImage(
                        array=rendered,
                        page=page_number,
                        source=ImageSource.RENDERED_PAGE,
                        label=f"page_{page_number}_rendered_{dpi}dpi",
                    )
                )
                harvest_result.images_examined += 1

            _collect_links(page, harvest_result)

            on_this_page = 0
            for xref, array in _embedded_images(document, page):
                if embedded_total >= max_total:
                    harvest_result.caps_reached.append(
                        f"max_embedded_images_total ({max_total}) reached"
                    )
                    break
                if on_this_page >= max_per_page:
                    harvest_result.caps_reached.append(
                        f"max_embedded_images_per_page ({max_per_page}) reached "
                        f"on page {page_number}"
                    )
                    break
                if array.shape[0] * array.shape[1] < min_pixels:
                    continue
                on_this_page += 1
                embedded_total += 1
                harvest_result.images_examined += 1
                harvest_result.images.append(
                    SourceImage(
                        array=array,
                        page=page_number,
                        source=ImageSource.EMBEDDED_IMAGE,
                        label=f"page_{page_number}_embedded_xref_{xref}",
                    )
                )

    # Deduplicate the cap notes: the same cap hit on twenty pages is one fact.
    harvest_result.caps_reached = list(dict.fromkeys(harvest_result.caps_reached))
    return harvest_result


def _render_page(page: Any, dpi: int) -> np.ndarray | None:
    try:
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    except Exception:
        return None
    try:
        array = np.frombuffer(pixmap.samples, dtype=np.uint8)
        array = array.reshape(pixmap.height, pixmap.width, pixmap.n)
    except Exception:
        return None
    if pixmap.n == 1:
        return array[:, :, 0]
    return np.ascontiguousarray(array[:, :, :3])


def _embedded_images(document: Any, page: Any) -> list[tuple[int, np.ndarray]]:
    import fitz

    found: list[tuple[int, np.ndarray]] = []
    seen: set[int] = set()
    try:
        entries = page.get_images(full=True)
    except Exception:
        return found

    for entry in entries:
        xref = entry[0]
        if xref in seen:
            continue
        seen.add(xref)
        try:
            pixmap = fitz.Pixmap(document, xref)
            # A CMYK or spot-colour image has to be converted before it can be
            # read as RGB bytes.
            if pixmap.n > 3 or pixmap.colorspace is None:
                pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
            array = np.frombuffer(pixmap.samples, dtype=np.uint8)
            array = array.reshape(pixmap.height, pixmap.width, pixmap.n)
            if pixmap.n == 1:
                array = array[:, :, 0]
            else:
                array = np.ascontiguousarray(array[:, :, :3])
        except Exception:
            continue
        found.append((xref, array))
    return found


def _collect_links(page: Any, result: ImageHarvest) -> None:
    """Record the hosts of clickable links, for the QR-vs-link cross-check.

    Reading the annotation only. Nothing here resolves or fetches a URL.
    """
    from .payload import parse_url

    try:
        links = page.get_links()
    except Exception:
        return

    for link in links or []:
        uri = link.get("uri")
        if not uri:
            continue
        result.link_uris.append(uri)
        parsed = parse_url(uri)
        if parsed.host:
            result.link_hosts.append(parsed.host)

    result.link_hosts = list(dict.fromkeys(result.link_hosts))
    result.link_uris = list(dict.fromkeys(result.link_uris))


def render_page_image(path: Path, page_number: int, dpi: int) -> Any:
    """Render one page as a PIL image, reusing this module's PDF rendering.

    Step 6's duplicate detection needs page 1 as an image, and rendering it a
    second way would mean two code paths that could drift: a page hashed under
    one renderer and compared against a corpus hashed under another produces
    distances that mean nothing. So both go through `_render_page`.

    An image file is returned as itself; `page_number` is then ignored.
    """
    from PIL import Image, UnidentifiedImageError

    if path.suffix.lower() != ".pdf":
        try:
            with Image.open(path) as handle:
                return handle.convert("RGB")
        except UnidentifiedImageError as exc:
            raise ImageLoadError(f"not a readable image: {exc}") from exc
        except OSError as exc:
            raise ImageLoadError(f"could not read image: {exc}") from exc

    import fitz

    try:
        document = fitz.open(path)
    except Exception as exc:
        raise ImageLoadError(f"could not open PDF: {exc}") from exc

    with document:
        if document.needs_pass:
            raise PasswordProtectedError("PDF is password protected")
        if page_number < 1 or page_number > document.page_count:
            raise ImageLoadError(
                f"page {page_number} is outside the document "
                f"(1-{document.page_count})"
            )
        array = _render_page(document.load_page(page_number - 1), dpi)

    if array is None:
        raise ImageLoadError(f"page {page_number} could not be rendered")

    # No explicit `mode`: Pillow deprecated the argument, and it infers both
    # of these correctly from the array's shape and dtype anyway.
    if array.ndim == 2:
        return Image.fromarray(array).convert("RGB")
    return Image.fromarray(array)
