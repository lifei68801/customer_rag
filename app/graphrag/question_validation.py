from __future__ import annotations

from app.graphrag.ontology import Term


def find_unmatched_questions(questions: list[str], terms: list[Term]) -> list[str]:
    """挑出「一个已知名字都没提到」的那几条引导问题。

    判据是「至少提到一个已知实体名、别名或实体类型名」。这不是完整的可答性
    证明——那要真跑一遍问答管线，慢，而且会在保存按钮上烧 LLM 调用。但它挡住
    绝大多数「问了个本体里根本没有的东西」，比如没有「库存」类型时的
    「库存多少？」。

    实体类型名也算：「产品有哪些口味？」里的两个词都是类型名不是实体名，
    只认实体名的话，本体里最典型的那类问题会被判成不可答。

    大小写不敏感：「beer 是什么」和「Beer 是什么」是同一个问题，
    敏感匹配只会让审核员反复困惑于为什么保存不了。
    """
    known: set[str] = set()
    for term in terms:
        known.add(term.standard_name.lower())
        known.update(alias.lower() for alias in term.aliases)
        known.add(term.term_type.lower())
    unmatched: list[str] = []
    for question in questions:
        lowered = question.lower()
        if not any(name and name in lowered for name in known):
            unmatched.append(question)
    return unmatched
