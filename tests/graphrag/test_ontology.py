from pathlib import Path

from app.graphrag.ontology import Term, load_terminology


def test_load_terminology_parses_terms_with_aliases(tmp_path):
    yaml_path = tmp_path / "terminology.yaml"
    yaml_path.write_text(
        "terms:\n"
        "  - standard_name: 错误码E502\n"
        "    aliases: [网关超时, E502]\n"
        "    term_type: error_code\n"
        "    product_line: 核心平台\n",
        encoding="utf-8",
    )

    terms = load_terminology(yaml_path)

    assert terms == [
        Term(
            standard_name="错误码E502",
            aliases=["网关超时", "E502"],
            term_type="error_code",
            product_line="核心平台",
        )
    ]
