from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EvalCase:
    question: str
    expected_answer: str
    expected_sources: list[str]


def load_eval_cases(path: Path) -> list[EvalCase]:
    """从 JSONL 文件加载评测集，每行一条 {question, expected_answer, expected_sources}。

    空行和以 # 开头的行会被跳过，便于在占位/示例数据集里写免责声明注释。
    """
    cases: list[EvalCase] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        data = json.loads(stripped)
        cases.append(
            EvalCase(
                question=str(data["question"]),
                expected_answer=str(data["expected_answer"]),
                expected_sources=[str(s) for s in data.get("expected_sources", [])],
            )
        )
    return cases
