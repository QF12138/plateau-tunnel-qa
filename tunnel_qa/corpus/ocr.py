import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


PAGE_MARKER = re.compile(r"(?m)^\[第(\d+)页\]\s*$")


@dataclass
class OCRResult:
    source: str
    output: str
    page_count: int
    ocr_pages: int
    embedded_text_pages: int
    text_chars: int
    errors: list[str]

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


def completed_page_count(text: str) -> int:
    """Return the last fully checkpointed page number in a partial OCR file."""
    markers = [int(match.group(1)) for match in PAGE_MARKER.finditer(text)]
    return max(markers, default=0)


def ordered_ocr_text(result: list | None, *, minimum_confidence: float = 0.45) -> str:
    """Normalize RapidOCR output into stable top-to-bottom, left-to-right text."""
    if not result:
        return ""
    lines: list[tuple[float, float, str]] = []
    for entry in result:
        if len(entry) < 3:
            continue
        box, text, confidence = entry[0], str(entry[1]).strip(), float(entry[2])
        if not text or confidence < minimum_confidence:
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        lines.append((min(ys), min(xs), text))
    lines.sort(key=lambda item: (round(item[0] / 12), item[1], item[0]))
    return "\n".join(text for _, _, text in lines)


class PDFOCRProcessor:
    """OCR scanned PDFs while retaining deterministic page markers and checkpoints."""

    def __init__(
        self,
        *,
        render_scale: float = 1.5,
        minimum_confidence: float = 0.45,
        embedded_text_threshold: int = 120,
    ) -> None:
        self.render_scale = render_scale
        self.minimum_confidence = minimum_confidence
        self.embedded_text_threshold = embedded_text_threshold

    def process(self, source: Path, output: Path, *, progress: bool = False) -> OCRResult:
        try:
            import numpy as np
            import pymupdf
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise RuntimeError(
                "OCR 组件未安装，请安装 pymupdf 与 rapidocr_onnxruntime"
            ) from exc

        source = source.resolve()
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_suffix(output.suffix + ".partial")
        if output.exists():
            existing = output.read_text(encoding="utf-8")
            with pymupdf.open(source) as document:
                if completed_page_count(existing) >= document.page_count:
                    return OCRResult(
                        source=str(source),
                        output=str(output),
                        page_count=document.page_count,
                        ocr_pages=document.page_count,
                        embedded_text_pages=0,
                        text_chars=len(existing),
                        errors=[],
                    )

        checkpoint = partial.read_text(encoding="utf-8") if partial.exists() else ""
        completed = completed_page_count(checkpoint)
        mode = "a" if completed else "w"
        engine = RapidOCR()
        errors: list[str] = []
        ocr_pages = 0
        embedded_pages = 0

        with pymupdf.open(source) as document, partial.open(mode, encoding="utf-8", newline="\n") as stream:
            for page_index in range(completed, document.page_count):
                page_number = page_index + 1
                page = document[page_index]
                embedded = (page.get_text() or "").strip()
                if len(embedded) >= self.embedded_text_threshold:
                    page_text = embedded
                    embedded_pages += 1
                else:
                    try:
                        pixmap = page.get_pixmap(
                            matrix=pymupdf.Matrix(self.render_scale, self.render_scale),
                            colorspace=pymupdf.csRGB,
                            alpha=False,
                        )
                        image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                            pixmap.height, pixmap.width, pixmap.n
                        )
                        result, _ = engine(image)
                        page_text = ordered_ocr_text(
                            result, minimum_confidence=self.minimum_confidence
                        )
                        ocr_pages += 1
                    except Exception as exc:  # retain the page marker and continue the batch
                        page_text = embedded
                        errors.append(f"page_{page_number}:{type(exc).__name__}:{exc}")
                stream.write(f"[第{page_number}页]\n{page_text}\n\n")
                stream.flush()
                if progress and (page_number == 1 or page_number % 10 == 0 or page_number == document.page_count):
                    print(
                        json.dumps(
                            {
                                "file": source.name,
                                "page": page_number,
                                "pages": document.page_count,
                                "ocr_chars": len(page_text),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            page_count = document.page_count

        partial.replace(output)
        text = output.read_text(encoding="utf-8")
        result = OCRResult(
            source=str(source),
            output=str(output),
            page_count=page_count,
            ocr_pages=ocr_pages,
            embedded_text_pages=embedded_pages,
            text_chars=len(text),
            errors=errors,
        )
        return result
