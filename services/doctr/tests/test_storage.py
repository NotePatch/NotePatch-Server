from doctr_service.storage import detect_image_format, non_image_extension


def test_non_image_extension_is_sanitized():
    assert non_image_extension("homework.pdf") == ".pdf"
    assert non_image_extension("bad.longextensionname") == ".bin"


def test_detect_image_format_rejects_text():
    assert detect_image_format(b"not an image") is None
