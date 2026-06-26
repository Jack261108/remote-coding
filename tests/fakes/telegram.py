from types import SimpleNamespace

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup


class DummyAnswerMessage:
    def __init__(self, text: str, *, reply_markup=None, parse_mode=None, fail_next_edit: bool = False) -> None:
        self.text = text
        self.reply_markup = reply_markup
        self.parse_mode = parse_mode
        self.edits: list[str] = []
        self.edit_parse_modes: list[ParseMode | None] = []
        self.fail_next_edit = fail_next_edit
        self.deleted = False
        self.chat = SimpleNamespace(id=1)
        self.message_id = 1

    async def edit_text(self, text: str, parse_mode=None, reply_markup=None) -> "DummyAnswerMessage":
        if self.fail_next_edit:
            self.fail_next_edit = False
            raise TelegramBadRequest(method="editMessageText", message="message is not modified")
        self.text = text
        self.reply_markup = reply_markup
        self.edits.append(text)
        self.edit_parse_modes.append(parse_mode)
        return self

    async def delete(self) -> bool:
        self.deleted = True
        return True


class DummyMessage:
    def __init__(
        self,
        text: str | None = None,
        user_id: int = 1,
        *,
        fail_on_calls: set[int] | None = None,
        fail_on_texts: set[str] | None = None,
        fail_first_edit: bool = False,
    ) -> None:
        self.text = text
        self.html_text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []
        self.sent_messages: list[DummyAnswerMessage] = []
        self.sent_documents: list[dict] = []
        self.reply_markups: list[InlineKeyboardMarkup | None] = []
        self.parse_modes: list[ParseMode | None] = []
        self.edited_reply_markups: list[InlineKeyboardMarkup | None] = []
        self._answer_calls = 0
        self._fail_on_calls = fail_on_calls or set()
        self._fail_on_texts = fail_on_texts or set()
        self._fail_first_edit = fail_first_edit

    async def answer(self, text: str, reply_markup=None, parse_mode=None) -> DummyAnswerMessage:
        self._answer_calls += 1
        if self._answer_calls in self._fail_on_calls or any(fragment in text for fragment in self._fail_on_texts):
            raise TelegramBadRequest(method="sendMessage", message="chat not found")
        sent = DummyAnswerMessage(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            fail_next_edit=self._fail_first_edit and not self.sent_messages,
        )
        self.answers.append(text)
        self.sent_messages.append(sent)
        self.reply_markups.append(reply_markup)
        self.parse_modes.append(parse_mode)
        return sent

    async def edit_text(self, text: str, parse_mode=None, reply_markup=None) -> DummyAnswerMessage:
        sent = DummyAnswerMessage(text, reply_markup=reply_markup, parse_mode=parse_mode)
        self.sent_messages.append(sent)
        return sent

    async def edit_reply_markup(self, reply_markup=None) -> None:
        self.edited_reply_markups.append(reply_markup)

    async def answer_document(self, document, caption: str | None = None) -> DummyAnswerMessage:
        filename = getattr(document, "filename", None) or "unknown"
        self.sent_documents.append({"document": document, "filename": filename, "caption": caption})
        sent = DummyAnswerMessage(caption or "", reply_markup=None)
        self.sent_messages.append(sent)
        return sent


class DummyCallbackQuery:
    def __init__(self, data: str, *, user_id: int = 1, message: DummyMessage | None = None) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = message
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))
