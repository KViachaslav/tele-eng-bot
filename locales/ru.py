"""Русскоязычные тексты бота.

Здесь собраны все строки, которые видит пользователь: «магических» строк в
остальном коде быть не должно. Подстановки оформлены через ``str.format``.
"""
from __future__ import annotations

from config import (
    FIELD_ANTONYMS,
    FIELD_COLLOCATIONS,
    FIELD_DEFINITION,
    FIELD_EXAMPLE,
    FIELD_POS,
    FIELD_RELATED,
    FIELD_SYNONYMS,
    POS_ADJECTIVE,
    POS_ADVERB,
    POS_NOUN,
    POS_OTHER,
    POS_VERB,
    REVEAL_MODE_ON_BUTTON,
    REVEAL_MODE_SPOILER,
)

# ---------------------------------------------------------------------------
# Названия команд и кнопки reply-клавиатуры
# ---------------------------------------------------------------------------
CMD_START = "/start"
CMD_SETTINGS = "/settings"
CMD_PAUSE = "/pause"
CMD_RESUME = "/resume"
CMD_STATS = "/stats"
CMD_WORD = "/word"
CMD_HELP = "/help"
CMD_CANCEL = "/cancel"

#: Описания команд для меню Telegram (``set_my_commands``).
COMMAND_DESCRIPTIONS: dict[str, str] = {
    CMD_START: "Регистрация и приветствие",
    CMD_SETTINGS: "Меню настроек",
    CMD_PAUSE: "Поставить рассылку на паузу",
    CMD_RESUME: "Возобновить рассылку",
    CMD_STATS: "Статистика",
    CMD_WORD: "Получить слово вне расписания",
    CMD_CANCEL: "Прервать ввод",
    CMD_HELP: "Справка",
}

BTN_MENU_WORD = "🎲 Слово"
BTN_MENU_STATS = "📊 Статистика"
BTN_MENU_SETTINGS = "⚙️ Настройки"
BTN_MENU_PAUSE = "⏸ Пауза"
BTN_MENU_RESUME = "▶️ Возобновить"
BTN_MENU_HELP = "ℹ️ Справка"

# ---------------------------------------------------------------------------
# Инлайн-кнопки
# ---------------------------------------------------------------------------
BTN_SHOW = "👀 Показать"
BTN_KNOW = "✅ Знаю"
BTN_DONT_KNOW = "❌ Не знаю"
BTN_MANUAL = "✍️ Ввести вручную"
BTN_DEFAULT = "По умолчанию ({value})"
BTN_LIMIT_UNLIMITED = "♾ Без ограничения"
BTN_BACK = "⬅️ Назад"
BTN_CLOSE = "✖️ Закрыть"
BTN_SETTINGS_WORDS = "🔢 Слов в день"
BTN_SETTINGS_LIMIT = "📚 Лимит в изучении"
BTN_SETTINGS_WINDOW = "🕘 Временное окно"
BTN_SETTINGS_TIMEZONE = "🌍 Часовой пояс"
BTN_SETTINGS_FIELDS = "🧩 Поля в сообщении"
BTN_SETTINGS_POS = "🏷 Часть речи"
BTN_SETTINGS_MODE = "👁 Режим показа"
BTN_POS_ALL = "Все части речи"
BTN_MODE_SPOILER = "Всё сразу (спойлер)"
BTN_MODE_ON_BUTTON = "Сначала слово, потом перевод"
BTN_WINDOW_PRESET = "{start}–{end}"

CHECKED_MARK = "✅"
UNCHECKED_MARK = "⬜"

# ---------------------------------------------------------------------------
# Подписи полей карточки
# ---------------------------------------------------------------------------
LABEL_DEFINITION = "📖"
LABEL_EXAMPLE = "✍️"
LABEL_POS = "🏷"
LABEL_RELATED = "🧬"
LABEL_COLLOCATIONS = "🔗"
LABEL_SYNONYMS = "🟰"
LABEL_ANTONYMS = "↔️"

FIELD_TITLES: dict[str, str] = {
    FIELD_DEFINITION: "📖 Definition (определение)",
    FIELD_EXAMPLE: "✍️ Example Sentence (пример)",
    FIELD_POS: "🏷 Part of Speech (часть речи)",
    FIELD_RELATED: "🧬 Related Forms (однокоренные)",
    FIELD_SYNONYMS: "🟰 Synonyms (синонимы)",
    FIELD_ANTONYMS: "↔️ Antonyms (антонимы)",
    FIELD_COLLOCATIONS: "🔗 Collocations (коллокации)",
}

POS_TITLES: dict[str, str] = {
    POS_NOUN: "noun",
    POS_VERB: "verb",
    POS_ADJECTIVE: "adjective",
    POS_ADVERB: "adverb",
    POS_OTHER: "other",
}

REVEAL_MODE_TITLES: dict[str, str] = {
    REVEAL_MODE_ON_BUTTON: BTN_MODE_ON_BUTTON,
    REVEAL_MODE_SPOILER: BTN_MODE_SPOILER,
}

# ---------------------------------------------------------------------------
# Приветствие / справка
# ---------------------------------------------------------------------------
START_NEW_USER = (
    "👋 Привет, {name}!\n\n"
    "Я помогу выучить английские слова из списка Oxford 3000.\n"
    "Буду присылать слова по расписанию, а ты отмечай кнопками "
    "«{know}» / «{dont_know}» — так я пойму, когда показать слово снова.\n\n"
    "Давай настроим бота. Шаг 1 из 3."
)

START_EXISTING_USER = (
    "👋 С возвращением, {name}!\n\n"
    "Бот уже настроен. Слова приходят ежедневно с {window_start} до {window_end} "
    "({words_per_day} слов в день, часовой пояс {timezone}).\n\n"
    "Доступные команды:\n{commands}"
)

HELP_TEXT = (
    "ℹ️ Справка\n\n"
    "Бот присылает слова из Oxford 3000 по расписанию. В сообщении есть перевод "
    "(спрятан под спойлер или показывается по кнопке), определение, пример, "
    "коллокации, синонимы и антонимы.\n\n"
    "Кнопки ответа:\n"
    "• {know} — слово становится на следующую ступень повторения;\n"
    "• {dont_know} — слово возвращается на первую ступень и придёт снова в ближайшее окно.\n\n"
    "Команды:\n"
    "{commands}\n\n"
    "Изученные слова повторяются раз в 90 дней и не расходуют дневной лимит.\n\n"
    "Новые слова приходят случайным образом и только пока слов в изучении меньше "
    "лимита (меняется в меню настроек)."
)

COMMANDS_TABLE = (
    "{start} — регистрация и приветствие\n"
    "{settings} — меню настроек\n"
    "{pause} — поставить рассылку на паузу\n"
    "{resume} — возобновить рассылку\n"
    "{stats} — статистика\n"
    "{word} — получить слово вне расписания\n"
    "{cancel} — прервать ввод\n"
    "{help} — эта справка"
)

NOT_REGISTERED = "Сначала выполни {start} — нужно выбрать часовой пояс и расписание."

# ---------------------------------------------------------------------------
# Регистрация
# ---------------------------------------------------------------------------
ASK_TIMEZONE = (
    "Шаг 1 из 3. 🌍 Выбери свой часовой пояс.\n\n"
    "Он нужен, чтобы слова приходили в твоё время суток. Можно выбрать один из "
    "популярных вариантов или нажать «{manual}» и прислать название "
    "в формате IANA, например: Europe/Moscow."
)

ASK_TIMEZONE_MANUAL = (
    "Пришли часовой пояс в формате IANA, например: Europe/Moscow, Asia/Almaty, UTC."
)

TIMEZONE_INVALID = (
    "❌ Не знаю такой часовой пояс: {value}.\n"
    "Проверь название (например, Europe/Moscow) и пришли его ещё раз."
)

ASK_WORDS_PER_DAY = (
    "Шаг 2 из 3. 🔢 Сколько слов присылать каждый день?\n\n"
    "Допустимо от {min_words} до {max_words}. Значение по умолчанию — {default_words}."
)

WORDS_PER_DAY_INVALID = (
    "❌ Нужно целое число от {min_words} до {max_words}. Пришли его ещё раз."
)

ASK_WINDOW = (
    "Шаг 3 из 3. 🕘 В какое время присылать слова?\n\n"
    "Выбери готовый вариант или укажи своё окно: сначала начало, потом конец."
)

ASK_WINDOW_START = "Пришли время начала окна в формате ЧЧ:ММ, например 09:00."

ASK_WINDOW_END = (
    "Теперь время окончания окна в формате ЧЧ:ММ, например 21:00. "
    "Оно должно быть позже начала ({start})."
)

WINDOW_INVALID = "❌ Не похоже на время. Нужен формат ЧЧ:ММ, например 09:00."

WINDOW_TOO_SHORT = (
    "❌ Окно слишком короткое: чтобы присылать {words_per_day} слов, нужно "
    "минимум {min_minutes} мин (сейчас {current_minutes} мин). "
    "Выбери окно пошире или уменьши количество слов."
)

WINDOW_END_BEFORE_START = "❌ Конец окна должен быть позже начала ({start})."

REGISTRATION_DONE = (
    "✅ Готово, настройки сохранены!\n\n"
    "🌍 Часовой пояс: {timezone}\n"
    "🔢 Слов в день: {words_per_day}\n"
    "🕘 Временное окно: {window_start}–{window_end}\n"
    "👁 Режим показа: {reveal_mode}\n\n"
    "Первое слово придёт в ближайший слот внутри окна. Если не хочешь ждать — "
    "нажми «{word_button}» или отправь {word_command}."
)

REGISTRATION_CANCELED = "Регистрация прервана. Отправь {start}, чтобы начать заново."

# ---------------------------------------------------------------------------
# Меню настроек
# ---------------------------------------------------------------------------
SETTINGS_MENU = (
    "⚙️ Настройки\n\n"
    "🌍 Часовой пояс: {timezone}\n"
    "🔢 Слов в день: {words_per_day}\n"
    "📚 Лимит в изучении: {learning_limit}\n"
    "🕘 Временное окно: {window_start}–{window_end}\n"
    "👁 Режим показа: {reveal_mode}\n"
    "🏷 Часть речи: {pos_filter}\n"
    "🧩 Поля в сообщении: {fields}\n"
    "⏯ Рассылка: {state}"
)

SETTINGS_STATE_ACTIVE = "активна"
SETTINGS_STATE_PAUSED = "на паузе"

ASK_WORDS_PER_DAY_CHANGE = (
    "🔢 Сколько слов присылать каждый день? Допустимо от {min_words} до {max_words}."
)

ASK_LEARNING_LIMIT_CHANGE = (
    "📚 Сколько слов держать в изучении одновременно? Пока таких слов не меньше "
    "лимита, новые слова не приходят — ни по расписанию, ни по кнопке «слово».\n\n"
    "Допустимо от {min_words} до {max_words} или «{unlimited}» — тогда лимита нет."
)

LEARNING_LIMIT_INVALID = (
    "❌ Нужно целое число от {min_words} до {max_words} "
    "(0 — без ограничения). Пришли его ещё раз."
)

LEARNING_LIMIT_UNLIMITED_TITLE = "без ограничения"

ASK_WINDOW_CHANGE = "🕘 Выбери временное окно или укажи своё (начало и конец)."

ASK_TIMEZONE_CHANGE = (
    "🌍 Выбери часовой пояс или нажми «{manual}» и пришли название в формате IANA."
)

FIELDS_MENU = (
    "🧩 Что показывать в сообщении со словом.\n\n"
    "Слово и перевод показываются всегда, коллокации можно отключить. "
    "Нажми на пункт, чтобы включить или выключить его."
)

POS_MENU = (
    "🏷 Слова какой части речи присылать?\n\n"
    "Можно выбрать несколько вариантов: {values}. "
    "«Все части речи» сбрасывает фильтр."
)

MODE_MENU = "👁 Как показывать перевод?"

MODE_SAVED = "✅ Режим показа: {value}"

SETTING_SAVED = "✅ Сохранено: {value}"

SETTINGS_CLOSED = "Настройки закрыты."

# ---------------------------------------------------------------------------
# Слова и ответы
# ---------------------------------------------------------------------------
WORD_NOT_FOUND = (
    "🤷 Не нашёл подходящего слова. Проверь фильтр по части речи или загляни позже."
)

NEW_WORDS_LIMIT_REACHED = (
    "📚 Лимит слов в изучении: {learning} из {limit}.\n\n"
    "Пока лимит не поднят, новые слова не приходят — но повторения уже начатых "
    "слов продолжают работать. Изменить лимит можно в настройках. Статистика: {stats}"
)

NO_WORDS_IN_DATABASE = (
    "📭 В базе пока нет слов. Запусти scripts/import_csv.py, чтобы загрузить словарь."
)

ANSWER_ALREADY_PROCESSED = "Это слово уже отмечено, ответ изменён не будет."

DELIVERY_NOT_FOUND = (
    "⚠️ Не нашёл это слово в истории отправок. Отправь {word_command}, чтобы получить новое."
)

REVEAL_INLINE_HINT = "Нажми «{show}», когда будешь готов увидеть перевод."

ANSWER_KNOW_RESULT = "✅ Ответ записан: знаю.\n{next_step}"

ANSWER_DONT_KNOW_RESULT = "❌ Ответ записан: не знаю.\n{next_step}"

NEXT_REVIEW_IN_DAYS = "Следующий показ: через {days} дн. (этап {stage})."

NEXT_REVIEW_TODAY = "Слово вернётся в ближайшую рассылку (этап 0)."

NEXT_REVIEW_LEARNED = (
    "Слово выучено 🎉 Вернётся на освежение через {days} дн. и не будет "
    "расходовать дневной лимит."
)

REFRESH_HINT = "🔁 Освежение: этот повтор не входит в дневной лимит."

SHOW_WORD_HINT = "Слово: {word}"

# ---------------------------------------------------------------------------
# Пауза / возобновление
# ---------------------------------------------------------------------------
PAUSED = (
    "⏸ Рассылка поставлена на паузу. Слова не будут приходить, пока ты не "
    "отправишь {resume}."
)

ALREADY_PAUSED = "⏸ Рассылка уже на паузе."

RESUMED = (
    "▶️ Рассылка возобновлена. Следующее слово придёт в ближайший слот "
    "внутри окна {window_start}–{window_end}."
)

ALREADY_ACTIVE = "▶️ Рассылка и так активна."

NEXT_WORD_AT = "\n\n⏰ Ближайшее слово: {moment} ({timezone})."

# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------
STATS_TEXT = (
    "📊 Статистика\n\n"
    "Всего слов в базе: {total_words}\n"
    "🎓 Изучено: {learned}\n"
    "📗 В процессе: {learning}\n"
    "🆕 Новых (ещё не отмечено): {new}\n"
    "📚 Лимит в изучении: {limit}\n\n"
    "🎯 Точность ответов: {accuracy}\n"
    "✅ Правильных ответов: {correct}\n"
    "❌ Ошибок: {wrong}\n"
    "🔥 Серия дней подряд: {streak}\n\n"
    "Самые проблемные слова:\n{problems}"
)

STATS_NO_PROBLEMS = "— пока нет слов с ошибками"

STATS_PROBLEM_LINE = "{index}. {word} — ошибок: {wrong}, правильных: {correct}"

STATS_ACCURACY_TEMPLATE = "{value}%"

# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------
ERROR_GENERIC = "⚠️ Что-то пошло не так. Я записал ошибку в лог и продолжаю работу."

ERROR_EMPTY_MESSAGE = "Пришли, пожалуйста, текстовое значение."

BOT_TOKEN_MISSING = (
    "Не задан BOT_TOKEN. Скопируй .env.example в .env и укажи токен от @BotFather."
)

# ---------------------------------------------------------------------------
# Импорт CSV (используется скриптом и логами)
# ---------------------------------------------------------------------------
IMPORT_STARTED = "Импорт слов из {path} ..."

IMPORT_FINISHED = (
    "Импорт завершён: всего строк {total}, добавлено {imported}, "
    "уже было {skipped}, дублей в файле {duplicates}, без слова {invalid}."
)

IMPORT_FILE_NOT_FOUND = "Файл не найден: {path}"

IMPORT_AUTO_STARTED = "Таблица words пуста — запускаю автоматический импорт из {path}."

IMPORT_CLI_DESCRIPTION = "Загружает слова Oxford 3000 из CSV-файла в базу бота."

IMPORT_CLI_PATH_HELP = (
    "Путь к CSV-файлу. Если не указан, берётся значение CSV_PATH из настроек."
)

IMPORT_CLI_ENCODING_HELP = (
    "Кодировка файла (utf-8, cp1251 ...). По умолчанию определяется автоматически."
)

IMPORT_CLI_BAD_FORMAT = "❌ Файл не похож на словарь Oxford 3000: {error}"

IMPORT_CLI_USAGE = "Пример: python scripts/import_csv.py data/oxford3000.csv"


# ---------------------------------------------------------------------------
# Готовые тексты с подстановками (чтобы форматирование жило рядом с текстами)
# ---------------------------------------------------------------------------
def render_learning_limit(value: int) -> str:
    """Лимит слов в изучении для экранов бота (``0`` — «без ограничения»)."""
    return LEARNING_LIMIT_UNLIMITED_TITLE if int(value or 0) <= 0 else str(value)


def render_commands() -> str:
    """Список команд с описаниями."""
    return COMMANDS_TABLE.format(
        start=CMD_START,
        settings=CMD_SETTINGS,
        pause=CMD_PAUSE,
        resume=CMD_RESUME,
        stats=CMD_STATS,
        word=CMD_WORD,
        cancel=CMD_CANCEL,
        help=CMD_HELP,
    )


def render_help() -> str:
    """Полный текст справки."""
    return HELP_TEXT.format(
        know=BTN_KNOW,
        dont_know=BTN_DONT_KNOW,
        commands=render_commands(),
    )


def render_start_existing(
    *,
    name: str,
    timezone: str,
    words_per_day: int,
    window_start: str,
    window_end: str,
) -> str:
    """Приветствие для уже настроенного пользователя."""
    return START_EXISTING_USER.format(
        name=name,
        timezone=timezone,
        words_per_day=words_per_day,
        window_start=window_start,
        window_end=window_end,
        commands=render_commands(),
    )


def render_not_registered() -> str:
    """Подсказка пользователю, который ещё не прошёл регистрацию."""
    return NOT_REGISTERED.format(start=CMD_START)


def render_delivery_not_found() -> str:
    """Подсказка, если кнопка относится к неизвестной отправке."""
    return DELIVERY_NOT_FOUND.format(word_command=CMD_WORD)
