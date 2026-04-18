from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UserQuestionOption:
    label: str
    description: str | None = None


@dataclass(frozen=True)
class UserQuestionPrompt:
    tool_use_id: str
    question_index: int
    total_questions: int
    question: str
    header: str | None = None
    options: tuple[UserQuestionOption, ...] = ()
    multi_select: bool = False

    @property
    def key(self) -> str:
        return f"{self.tool_use_id}:{self.question_index}"


def extract_user_question_prompts(
    *,
    tool_use_id: str,
    tool_name: str | None,
    tool_input: dict[str, Any] | None,
) -> tuple[UserQuestionPrompt, ...]:
    if not tool_use_id:
        return ()
    if (tool_name or "").strip().lower() != "askuserquestion":
        return ()
    if not isinstance(tool_input, dict):
        return ()

    raw_questions = tool_input.get("questions")
    if not isinstance(raw_questions, list):
        return ()

    prompts: list[UserQuestionPrompt] = []
    total_questions = len(raw_questions)
    for index, raw_question in enumerate(raw_questions):
        if not isinstance(raw_question, dict):
            continue
        question = str(raw_question.get("question") or "").strip()
        if not question:
            continue

        options: list[UserQuestionOption] = []
        raw_options = raw_question.get("options")
        if isinstance(raw_options, list):
            for raw_option in raw_options:
                if not isinstance(raw_option, dict):
                    continue
                label = str(raw_option.get("label") or "").strip()
                if not label:
                    continue
                description = raw_option.get("description")
                options.append(
                    UserQuestionOption(
                        label=label,
                        description=str(description).strip() if description is not None and str(description).strip() else None,
                    )
                )

        header = raw_question.get("header")
        prompts.append(
            UserQuestionPrompt(
                tool_use_id=tool_use_id,
                question_index=index,
                total_questions=total_questions,
                question=question,
                header=str(header).strip() if header is not None and str(header).strip() else None,
                options=tuple(options),
                multi_select=bool(raw_question.get("multiSelect", False)),
            )
        )

    return tuple(prompts)


def compose_user_question_answers(
    prompts: tuple[UserQuestionPrompt, ...],
    answers_by_index: dict[int, str],
) -> str:
    if not prompts:
        return ""
    if len(prompts) == 1:
        return answers_by_index.get(prompts[0].question_index, "").strip()

    lines = ["我的选择如下："]
    for prompt in prompts:
        answer = answers_by_index.get(prompt.question_index, "").strip()
        if not answer:
            continue
        title = prompt.header or prompt.question
        lines.append(f"- {title}: {answer}")
    return "\n".join(lines).strip()
