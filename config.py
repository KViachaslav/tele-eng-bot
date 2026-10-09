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
CALLBACK_AUDIO = "audio"

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
ACTION_FORECAST = "forecast"

# режимы показа (users.reveal_mode)
REVEAL_MODE_ON_BUTTON = "on_button"  # «сначала слово, потом перевод» (по умолчанию)
REVEAL_MODE_SPOILER = "spoiler"  # «всё сразу», перевод под спойлером
REVEAL_MODES = (REVEAL_MODE_ON_BUTTON, REVEAL_MODE_SPOILER)

# ответы пользователя (delivery_log.answer)
ANSWER_KNOW = "know"
ANSWER_DONT_KNOW = "dont_know"
ANSWERS = (ANSWER_KNOW, ANSWER_DONT_KNOW)

# ---------------------------------------------------------------------------
# Озвучка слов
# ---------------------------------------------------------------------------
#: Каталог с mp3: файлы лежат как ``data/<акцент>/<слово>_<акцент>.mp3``
#: (см. :mod:`services.audio`).
AUDIO_DIR: Path = DATA_DIR

AUDIO_ACCENT_UK = "uk"
AUDIO_ACCENT_US = "us"
#: Акценты в порядке кнопок озвучки в карточке слова (``🔊 🇬🇧 UK``, ``🔊 🇺🇸 US``).
#: Акцент хранится в ``callback_data`` кнопки, поэтому каждый вариант озвучки
#: запрашивается отдельно (см. :mod:`services.audio`).
AUDIO_ACCENTS: tuple[str, ...] = (AUDIO_ACCENT_UK, AUDIO_ACCENT_US)

# ---------------------------------------------------------------------------
# Перевод определений и примеров (MyMemory API)
# ---------------------------------------------------------------------------
#: Словарь Oxford 3000 англоязычный: определение и пример приходят на английском.
#: Русские версии этих строк дозапрашиваются у бесплатного MyMemory и лежат в
#: ``words.russian_definition`` / ``words.russian_example`` (см.
#: :mod:`services.translator`). В сообщении они идут под английскими строками и
#: всегда под спойлером Telegram.
TRANSLATION_API_URL = "https://api.mymemory.translated.net/get"
TRANSLATION_SOURCE_LANG = "en"
TRANSLATION_TARGET_LANG = "ru"
#: Сколько секунд ждать ответ сервиса: перевод не должен задерживать карточку
#: дольше, чем на пару таймаутов.
TRANSLATION_TIMEOUT_SECONDS = 5.0
#: Предельная длина одной строки: MyMemory не переводит длинные тексты, а
#: определения и примеры Oxford 3000 заметно короче (лимит API — 500 символов).
TRANSLATION_TEXT_LIMIT = 500
#: Пауза после первой неудачи: пока она идёт, перевод не запрашивается. Без неё
#: каждая карточка ждала бы таймаута сети (например, у сервера нет доступа в
#: интернет или исчерпан дневной лимит MyMemory).
TRANSLATION_FAILURE_COOLDOWN_SECONDS = 600
#: Суточные лимиты MyMemory в символах: без указанной почты сервис переводит
#: мало, с почтой (``MYMEMORY_EMAIL``, параметр ``de``) — в десять раз больше.
#: Нужны для отчёта пакетного перевода: на сколько ещё суток хватит словаря.
MYMEMORY_DAILY_CHARS_ANONYMOUS = 5000
MYMEMORY_DAILY_CHARS_WITH_EMAIL = 50000
#: Пакетный перевод словаря (``scripts/translate_words.py``): сколько слов брать
#: за один прогон. На слово уходит ≈95 символов (определение и пример), поэтому
#: 500 слов — это примерно суточная порция с почтой (≈50000 символов); без неё
#: порцию придётся уменьшать примерно в десять раз (``--limit``).
TRANSLATION_BATCH_LIMIT = 500
#: Пауза между запросами пакетного перевода: бесплатный сервис не любит частые
#: обращения, а порция из 500 слов и так занимает несколько минут.
TRANSLATION_BATCH_SLEEP_SECONDS = 0.6
#: Сколько неудач подряд считать исчерпанным суточным лимитом и остановить
#: прогон: переведённое уже лежит в базе, продолжить можно в следующий раз.
TRANSLATION_BATCH_MAX_FAILURES = 3
#: Сколько секунд ждать ответ сервиса в пакетном переводе. Карточке важен темп
#: (см. :data:`TRANSLATION_TIMEOUT_SECONDS`), а фоновому скрипту — результат:
#: изредка MyMemory отвечает медленно, и на пяти секундах порция зря встаёт.
TRANSLATION_BATCH_TIMEOUT_SECONDS = 20.0

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
#: Формат даты в коротких текстах: «15.01» (экран «📅 План слов»).
DATE_FORMAT = "%d.%m"
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
# Ручной запрос «дай слово» (``/word`` и кнопка «🎲 Слово»)
# ---------------------------------------------------------------------------
#: Сколько раз за местные сутки карточка слова вообще может уйти пользователю —
#: любым источником: расписанием, ручным запросом или их сочетанием. Лимит
#: ограничивает **повторный** проход кнопки «🎲 Слово»: слова дня, которые уже
#: приходили и получили ответ, можно пройти заново, но не бесконечно. План дня
#: лимитом не ограничен: слово уже приходило, а пункт плана оно всё ещё занимает,
#: поэтому молчать о нём нельзя (см. :mod:`services.delivery`).
ON_DEMAND_MAX_SENDS_PER_DAY = 2

# ---------------------------------------------------------------------------
# Порядок очереди слов (раздел «Порядок очереди» ТЗ)
# ---------------------------------------------------------------------------
#: Чем меньше ранг, тем раньше слово выбирается в очередь:
#: освежение выученного (этап 6) → этапы 5…1 → этап 0 «не сегодня» → новое слово →
#: этап 0 «сегодня» (самый последний: ответ «не знаю» возвращает слово в очередь
#: сразу, и подряд оно приходить не должно).
QUEUE_RANK_LEARNED = 10
#: База ранга для этапов 5…1: ранг = ``QUEUE_RANK_STAGE_BASE`` − этап (15…19).
QUEUE_RANK_STAGE_BASE = 20
#: Этап 0, который в последний раз повторяли не сегодня (и ни разу до этого).
QUEUE_RANK_STAGE_ZERO_NOT_TODAY = 25
#: Новое слово словаря — между этапом 0 «не сегодня» и этапом 0 «сегодня».
QUEUE_RANK_NEW_WORD = 30
#: Этап 0, повторённый сегодня: «не знаю» вернуло слово в очередь, но подряд оно
#: не приходит — сначала отдаются остальные ступени.
QUEUE_RANK_STAGE_ZERO_TODAY = 40

# ---------------------------------------------------------------------------
# Планировщик
# ---------------------------------------------------------------------------
MIN_SLOT_INTERVAL_MINUTES = 1
SCHEDULER_JOB_PREFIX = "user"
SCHEDULER_JOB_ID_TEMPLATE = SCHEDULER_JOB_PREFIX + ":{user_id}"
# допуск (в секундах) при проверке, что «сейчас» внутри окна рассылки
WINDOW_TOLERANCE_SECONDS = 90
#: Допуск опоздания задачи планировщика: если бот не работал меньше этого времени,
#: слот всё-таки выполняется — один раз (``coalesce``). Раньше допуск равнялся
#: :data:`WINDOW_TOLERANCE_SECONDS` (90 секунд), поэтому даже короткий рестарт
#: бота стоил пользователю слова: задача считалась «просроченной» и не выполнялась,
#: а новой не появлялось (см. ``services/scheduler.SchedulerService``).
SCHEDULER_MISFIRE_GRACE_SECONDS = 3600
#: Страж планировщика: как часто проверять, что у каждого активного пользователя
#: есть задача на будущее. Задача снимается вручную, теряется при ошибке расчёта
#: слота или при ручном удалении — тогда расписание пользователя исчезает вовсе,
#: и без стража это видно только по отсутствию слов (см.
#: :meth:`services.scheduler.SchedulerService.watchdog`).
SCHEDULER_WATCHDOG_SECONDS = 300
#: Идентификатор задачи стража: по нему видно, что страж жив (``/stats``, логи).
SCHEDULER_WATCHDOG_JOB_ID = "watchdog"
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
# Прогноз на несколько дней (экран «📅 План слов»)
# ---------------------------------------------------------------------------
#: Сколько дней показывает прогноз: сегодня плюс следующие ``FORECAST_DAYS - 1``.
FORECAST_DAYS = 3

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
    #: Перевод определений и примеров на русский (MyMemory API).
    translation_enabled: bool = True
    #: Почта для MyMemory: без неё сервис даёт меньше переводов в сутки.
    mymemory_email: str = ""

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
