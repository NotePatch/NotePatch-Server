from __future__ import annotations

from notepatch.modules.documents.ocr.base import OcrBlock, OcrDocumentResult, OcrPageResult


class MarkdownExporter:
    def page_plain_text(self, page: OcrPageResult) -> str:
        values = [self._block_text(block) for block in sorted(page.blocks, key=lambda item: item.reading_order)]
        return "\n".join(value for value in values if value).strip()

    def page_markdown(self, page: OcrPageResult) -> str:
        values = [self._block_markdown(block) for block in sorted(page.blocks, key=lambda item: item.reading_order)]
        return "\n\n".join(value for value in values if value).strip()

    def document_plain_text(self, pages: list[OcrPageResult]) -> str:
        return "\n\n".join(page.plain_text for page in pages if page.plain_text).strip()

    def document_markdown(self, pages: list[OcrPageResult]) -> str:
        rendered = []
        for page in pages:
            prefix = f"## Page {page.page_index + 1}"
            body = page.markdown or page.plain_text
            rendered.append(f"{prefix}\n\n{body}".strip())
        return "\n\n".join(rendered).strip()

    def export_text(self, result: OcrDocumentResult) -> str:
        return result.plain_text

    def export_markdown(self, result: OcrDocumentResult) -> str:
        return result.markdown

    @staticmethod
    def _block_text(block: OcrBlock) -> str:
        if block.type == "formula" and block.latex:
            return block.latex
        if block.type == "table":
            return block.markdown or block.html or block.text or ""
        return block.text or block.latex or block.markdown or ""

    @staticmethod
    def _block_markdown(block: OcrBlock) -> str:
        if block.type == "formula" and block.latex:
            return f"$$\n{block.latex}\n$$"
        if block.type == "table":
            return block.markdown or block.html or block.text or ""
        return block.markdown or block.text or block.latex or ""
