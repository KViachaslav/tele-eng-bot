"""Конфигурация приложения: переменные окружения (.env) и константы.

Все «магические» значения собраны в этом модуле, все тексты — в ``locales/ru.py``.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Пути
# ---------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).resolve().parent
DATA_DIR: Path = BASE_DIR / "data"
LOGS_DIR: Path = BASE_DIR / "logs"
ENV_FILE: Path = BASE_DIR / ".env"

DEFAULT_CSV_FILE_NAME = "oxford3000_vocabulary_with_collocations_and_definitions_datasets.csv"
DEFAULT_CSV_PATH: Path = DATA_DIR / DEFAULT_CSV_FILE_NAME
DEFAULT_DB_PATH: Path = BASE_DIR / "bot.db"

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
PARSE_MODE = "MarkdownV2"
CALLBACK_DELIMITER = ":"

# callback-префиксы
CALLBACK_REG = "reg"
CALLBACK_SETTINGS = "settings"
CALLBACK_SHOW = "show"
CALLBACK_ANSWER = "answer"

# callback-действия
ACTION_MANUAL = "manual"
ACTION_SKIP = "skip"
ACTION_BACK = "back"
ACTION_CLOSE = "close"
ACTION_TOGGLE = "toggle"

ACTION_WORDS_PER_DAY = "words"
ACTION_LEARNING_LIMIT = "limit"
ACTION_WINDOW = "window"
ACTION_TIMEZONE = "tz"
ACTION_FIELDS = "fields"
ACTION_POS = "pos"
ACTION_MODE = "mode"
ACTION_WINDOW_START = "win_start"
ACTION_WINDOW_END = "win_end"

# режимы показа (users.reveal_mode)
REVEAL_MODE_ON_BUTTON = "on_button"  # «сначала слово, потом перевод» (по умолчанию)
REVEAL_MODE_SPOILER = "spoiler"  # «всё сразу», перевод под спойлером
REVEAL_MODES = (REVEAL_MODE_ON_BUTTON, REVEAL_MODE_SPOILER)

# ответы пользователя (delivery_log.answer)
ANSWER_KNOW = "know"
ANSWER_DONT_KNOW = "dont_know"
ANSWERS = (ANSWER_KNOW, ANSWER_DONT_KNOW)

# ---------------------------------------------------------------------------
# Настройки пользователя по умолчанию
# ---------------------------------------------------------------------------
DEFAULT_TIMEZONE = "Europe/Moscow"
WORDS_PER_DAY_DEFAULT = 10
WORDS_PER_DAY_MIN = 1
WORDS_PER_DAY_MAX = 50

# Лимит слов, которые одновременно находятся «в изучении» (этапы 0–5).
# Пока таких слов не меньше лимита, новые слова не показываются: ни в рассылке
# по слотам, ни по запросу «дай слово». Значение 0 означает «без ограничения».
LEARNING_LIMIT_DEFAULT = 100
LEARNING_LIMIT_MIN = 10
LEARNING_LIMIT_MAX = 1000
LEARNING_LIMIT_UNLIMITED = 0
WINDOW_START_DEFAULT = "09:00"
WINDOW_END_DEFAULT = "21:00"
TIME_FORMAT = "%H:%M"
#: Разделитель начала и конца окна в callback-данных («0900-2100»).
WINDOW_VALUE_SEPARATOR = "-"

# популярные часовые пояса для инлайн-клавиатуры
TIMEZONE_PRESETS = (
    "Europe/Moscow",
    "Europe/Kyiv",
    "Europe/Minsk",
    "Asia/Almaty",
    "Asia/Tbilisi",
    "Asia/Yerevan",
    "Europe/Berlin",
    "Europe/London",
    "America/New_York",
    "Asia/Dubai",
    "Asia/Tokyo",
    "UTC",
)
WORDS_PER_DAY_PRESETS = (5, 10, 15, 20, 30)
LEARNING_LIMIT_PRESETS = (50, 100, 200, 500)
WINDOW_PRESETS = (
    ("07:00", "23:00"),
    ("08:00", "20:00"),
    ("09:00", "21:00"),
    ("10:00", "22:00"),
    ("12:00", "00:00"),
)

# ---------------------------------------------------------------------------
# Фильтр по части речи
# ---------------------------------------------------------------------------
POS_FILTER_ALL = "all"
POS_NOUN = "noun"
POS_VERB = "verb"
POS_ADJECTIVE = "adjective"
POS_ADVERB = "adverb"
POS_OTHER = "other"
POS_STANDARD_VALUES = (POS_NOUN, POS_VERB, POS_ADJECTIVE, POS_ADVERB)
POS_FILTER_VALUES = (*POS_STANDARD_VALUES, POS_OTHER)

# ---------------------------------------------------------------------------
# Поля карточки слова (user_settings)
# ---------------------------------------------------------------------------
FIELD_DEFINITION = "show_definition"
FIELD_EXAMPLE = "show_example"
FIELD_POS = "show_pos"
FIELD_RELATED = "show_related"
FIELD_SYNONYMS = "show_synonyms"
FIELD_ANTONYMS = "show_antonyms"
FIELD_COLLOCATIONS = "show_collocations"
FIELD_NAMES = (
    FIELD_DEFINITION,
    FIELD_EXAMPLE,
    FIELD_POS,
    FIELD_RELATED,
    FIELD_SYNONYMS,
    FIELD_ANTONYMS,
    FIELD_COLLOCATIONS,
)

# ---------------------------------------------------------------------------
# SRS
# ---------------------------------------------------------------------------
# Фиксированная таблица этапов: этап -> интервал в днях до следующего показа.
SRS_STAGE_INTERVALS_DAYS: dict[int, int] = {0: 0, 1: 1, 2: 3, 3: 7, 4: 14, 5: 30, 6: 90}
SRS_FIRST_STAGE = 0
SRS_MAX_STAGE = 6  # этап 6 == learned
SRS_MIN_INTERVAL_DAYS = 0

STATUS_NEW = "new"
STATUS_LEARNING = "learning"
STATUS_LEARNED = "learned"
STATUSES = (STATUS_NEW, STATUS_LEARNING, STATUS_LEARNED)

# ---------------------------------------------------------------------------
# Планировщик
# ---------------------------------------------------------------------------
MIN_SLOT_INTERVAL_MINUTES = 1
SCHEDULER_JOB_PREFIX = "user"
SCHEDULER_JOB_ID_TEMPLATE = SCHEDULER_JOB_PREFIX + ":{user_id}"
# допуск (в секундах) при проверке, что «сейчас» внутри окна рассылки
WINDOW_TOLERANCE_SECONDS = 90
# на сколько дней вперёд ищем ближайший слот (окно может быть уже закрыто сегодня)
SCHEDULER_LOOKAHEAD_DAYS = 3
MINUTES_PER_DAY = 24 * 60
SECONDS_PER_DAY = MINUTES_PER_DAY * 60

# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------
TOP_PROBLEM_WORDS_LIMIT = 5
STREAK_MAX_LOOKBACK_DAYS = 366

# ---------------------------------------------------------------------------
# Сообщения / форматирование
# ---------------------------------------------------------------------------
SPOILER_DELIMITER = "||"
WORD_TRANSLATION_SEPARATOR = " - "
EMPTY_VALUE_MARKER = "—"
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
IMPORT_CHUNK_SIZE = 500

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
DEFAULT_LOG_LEVEL = "INFO"
#: Формат строки лога: время, уровень, место в коде, сообщение.
LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} | {message}"
LOG_ROTATION = "10 MB"
LOG_RETENTION = "14 days"


class Settings(BaseSettings):
    """Настройки приложения, читаемые из ``.env`` (и переменных окружения).

    Имена полей совпадают с именами переменных окружения (регистр не важен).
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = ""
    db_path: Path = DEFAULT_DB_PATH
    csv_path: Path = DEFAULT_CSV_PATH
    log_level: str = DEFAULT_LOG_LEVEL
    log_file: Path | None = LOGS_DIR / "bot.log"
    default_timezone: str = DEFAULT_TIMEZONE

    def resolve_path(self, path: Path) -> Path:
        """Разворачивает относительный путь относительно каталога проекта."""
        return path if path.is_absolute() else (BASE_DIR / path)

    @property
    def database_path(self) -> Path:
        """Абсолютный путь к файлу базы данных SQLite."""
        return self.resolve_path(self.db_path)

    @property
    def dataset_path(self) -> Path:
        """Абсолютный путь к CSV с исходным словарём."""
        return self.resolve_path(self.csv_path)

    @property
    def log_file_path(self) -> Path | None:
        """Абсолютный путь к файлу логов (``None`` — писать только в консоль)."""
        if self.log_file is None:
            return None
        path = str(self.log_file).strip()
        if not path:
            return None
        return self.resolve_path(Path(path))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Возвращает единственный экземпляр настроек (кэшируется)."""
    return Settings()
