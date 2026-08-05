from app.ingestion.ocr_parser import parse_image


def test_parse_image_returns_a_single_chunk_with_ocr_text(tmp_path):
    image_path = tmp_path / "scan.png"
    image_path.write_bytes(b"fake image bytes")

    def fake_ocr(path):
        assert path == image_path
        return "错误码E502表示网关超时"

    chunks = parse_image(image_path, ocr=fake_ocr)

    assert len(chunks) == 1
    assert chunks[0].text == "错误码E502表示网关超时"
    assert chunks[0].heading_path == []
    assert chunks[0].source == str(image_path)


def test_parse_image_returns_empty_list_when_ocr_finds_no_text(tmp_path):
    image_path = tmp_path / "blank.png"
    image_path.write_bytes(b"fake image bytes")

    chunks = parse_image(image_path, ocr=lambda path: "   ")

    assert chunks == []
