"""Тесты поиска файлов озвучки (:mod:`services.audio`).

Файлы создаются в ``tmp_path``: тесты не зависят от 192 МБ архива
``data/uk`` и ``data/us`` и работают даже если каталогов озвучки нет вовсе.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import config
from services import audio


@pytest.fixture(autouse=True)
def clean_index_cache() -> None:
    """Индекс каталогов кэшируется — между тестами его нужно сбрасывать."""
    audio.clear_cache()
    yield
    audio.clear_cache()


def make_mp3(directory: Path, name: str) -> Path:
    """Создаёт фиктивный mp3 в каталоге акцента и возвращает путь."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"ID3\x03")
    return path


def test_find_audio_ignores_case(tmp_path: Path) -> None:
    """Слово из базы в другом регистре находит файл (на Linux это разные имена)."""
    file = make_mp3(tmp_path / "uk", "Internet_uk.mp3")

    assert audio.find_audio("Internet", "uk", directory=tmp_path) == file
    assert audio.find_audio("internet", "uk", directory=tmp_path) == file


def test_find_audio_matches_word_accents(tmp_path: Path) -> None:
    """Каждому акценту соответствует свой каталог: ``_us.mp3`` не подходит для uk."""
    file = make_mp3(tmp_path / "uk", "apple_uk.mp3")
    make_mp3(tmp_path / "us", "apple_us.mp3")

    assert audio.find_audio("apple", "uk", directory=tmp_path) == file
    assert audio.find_audio("apple", "us", directory=tmp_path) == tmp_path / "us" / "apple_us.mp3"


def test_find_audio_handles_spaces_and_hyphens(tmp_path: Path) -> None:
    """Словосочетание из файла («ice cream») находится и по дефису, и по подчёркиванию."""
    file = make_mp3(tmp_path / "uk", "ice cream_uk.mp3")

    for spelling in ("ice cream", "ice-cream", "ice_cream", "Ice Cream"):
        assert audio.find_audio(spelling, "uk", directory=tmp_path) == file


def test_find_audio_prefers_file_with_matching_case(tmp_path: Path) -> None:
    """Коллизия регистра из архива: «March_uk(1).mp3» — файл слова «March».

    Windows не различает регистр, поэтому ``March_uk.mp3`` и ``march_uk.mp3``
    распаковались как ``march_uk.mp3`` и ``March_uk(1).mp3``: слово «March»
    должно получить копию с хвостом, а «march» — обычное имя.
    """
    plain = make_mp3(tmp_path / "uk", "march_uk.mp3")
    duplicate = make_mp3(tmp_path / "uk", "March_uk(1).mp3")

    assert audio.find_audio("March", "uk", directory=tmp_path) == duplicate
    assert audio.find_audio("march", "uk", directory=tmp_path) == plain


def test_find_audio_prefers_plain_name_for_same_spelling(tmp_path: Path) -> None:
    """Одинаковое написание: выигрывает имя без хвоста «(1)»."""
    plain = make_mp3(tmp_path / "uk", "March_uk.mp3")
    make_mp3(tmp_path / "uk", "March_uk(1).mp3")

    assert audio.find_audio("March", "uk", directory=tmp_path) == plain


def test_find_audio_uses_duplicate_when_plain_absent(tmp_path: Path) -> None:
    """Если обычного имени в архиве нет, подходит и копия с хвостом «(1)»."""
    file = make_mp3(tmp_path / "uk", "IT_uk(1).mp3")

    assert audio.find_audio("IT", "uk", directory=tmp_path) == file


def test_find_audio_does_not_borrow_fused_word(tmp_path: Path) -> None:
    """«work out» и «workout» — разные слова: чужой файл не подставляется."""
    file = make_mp3(tmp_path / "uk", "workout_uk.mp3")
    make_mp3(tmp_path / "uk", "per cent_uk.mp3")

    assert audio.find_audio("work out", "uk", directory=tmp_path) is None
    assert audio.find_audio("percent", "uk", directory=tmp_path) is None
    assert audio.find_audio("workout", "uk", directory=tmp_path) == file


def test_find_audio_returns_none_for_unknown_word(tmp_path: Path) -> None:
    """Слова нет в архиве озвучки — возвращается ``None``, а не чужой файл."""
    make_mp3(tmp_path / "uk", "apple_uk.mp3")

    assert audio.find_audio("account for", "uk", directory=tmp_path) is None


def test_find_audio_returns_none_without_directory(tmp_path: Path) -> None:
    """Каталог озвучки не скопирован на сервер: поиск не падает."""
    assert audio.find_audio("apple", "uk", directory=tmp_path / "missing") is None


def test_find_audio_uses_directory_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без аргумента ``directory`` берётся каталог озвучки из конфига."""
    file = make_mp3(tmp_path / "uk", "apple_uk.mp3")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path)

    assert audio.find_audio("apple", config.AUDIO_ACCENT_UK) == file


def test_find_audio_does_not_substitute_other_accent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Файла запрошенного акцента нет — ``None``: другой акцент не подставляется."""
    make_mp3(tmp_path / "uk", "apple_uk.mp3")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path)

    assert audio.find_audio("apple", config.AUDIO_ACCENT_US) is None


#: Слова из настоящего архива: с регистром, словосочетания и копии «(1)».
REAL_WORDS = ("apple", "Internet", "TV", "o'clock", "ice cream", "March", "it")

#: Проверки на реальном архиве пропускаются, если ``data/uk`` не скопирован.
requires_archive = pytest.mark.skipif(
    not (config.DATA_DIR / config.AUDIO_ACCENT_UK).is_dir(),
    reason="каталог data/uk не скопирован",
)


@requires_archive
@pytest.mark.parametrize("word", REAL_WORDS)
def test_real_archive_files_are_found(word: str) -> None:
    """Файлы настоящего архива находятся: ``data/uk`` и ``data/us``.

    Проверка имён из реального архива: в нём есть слова в верхнем регистре
    (``TV``, ``Internet``), апостроф (``o'clock``), словосочетание с пробелом
    (``ice cream``) и копия с хвостом «(1)» (``March_uk(1).mp3``).
    """
    for accent in config.AUDIO_ACCENTS:
        assert audio.find_audio(word, accent, directory=config.DATA_DIR) is not None


@requires_archive
def test_real_archive_resolves_case_collisions() -> None:
    """Слова из коллизий регистра получают свои файлы, а не одноимённые.

    В архиве были и ``it_uk.mp3``, и ``IT_uk.mp3``; Windows при распаковке
    переименовала вторую коллизию в ``IT_uk(1).mp3``. Слово «it» из базы должно
    получить ``it_uk.mp3``, а «March» (месяц) — ``March_uk(1).mp3``, а не файл
    глагола ``march_uk.mp3``.
    """
    for accent in config.AUDIO_ACCENTS:
        assert audio.find_audio("it", accent, directory=config.DATA_DIR).name == f"it_{accent}.mp3"
        assert (
            audio.find_audio("March", accent, directory=config.DATA_DIR).name
            == f"March_{accent}(1).mp3"
        )


@requires_archive
@pytest.mark.parametrize("word", ("work out", "percent", "back up", "turn out"))
def test_real_archive_does_not_borrow_fused_word_audio(word: str) -> None:
    """У «слитных» пар своя озвучка только у того слова, что есть в архиве.

    В базе Oxford 3000 лежат и «work out», и «workout» — это разные слова.
    Файл в архиве есть только у слитного написания, поэтому фразовому глаголу
    чужой mp3 не подставляется: хендлер покажет алерт (см. ``handlers``).
    """
    for accent in config.AUDIO_ACCENTS:
        assert audio.find_audio(word, accent, directory=config.DATA_DIR) is None
