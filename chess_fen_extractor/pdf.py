"""Render PDF pages to images for board detection."""
import pymupdf


def pdf_pages_to_images(pdf_path, max_dim=2600):
    """Yield (page_number, np.ndarray BGR image) for each page of a PDF."""
    doc = pymupdf.open(pdf_path)
    try:
        for pno in range(len(doc)):
            page = doc[pno]
            mat = pymupdf.Matrix(1, 1)
            pix = page.get_pixmap(matrix=mat)
            if pix.width > max_dim or pix.height > max_dim:
                scale = max_dim / max(pix.width, pix.height)
                mat = pymupdf.Matrix(scale, scale)
                pix = page.get_pixmap(matrix=mat)
            import numpy as np
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n)
            if pix.n == 4:
                img = img[:, :, :3]
            elif pix.n == 1:
                img = np.repeat(img, 3, axis=2)
            yield pno + 1, img.copy()
    finally:
        doc.close()
