"""The UI's translation catalogues, checked against the code that uses
them.

``TranslationCatalog`` is ``Record<string, string>``, so TypeScript has
nothing to say about any of this: ``tsc`` is perfectly happy with a
``t('ai.save')`` that no catalogue defines, and equally happy with a
French catalogue a hundred keys behind the English one. The lookup is
``translations[language][key] ?? translations.en[key] ?? key`` — a miss
in French quietly serves English, and a miss in both renders the key
itself, so the button reads ``ai.save``.

That is the same shape of defect this repository has now fixed several
times: an enumerated list falling behind the thing it lists, with
nothing that notices. These tests live on the server side because the
frontend has no test runner; they only read files, so they cost nothing
and need no new tooling.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_LOCALES = _REPO / "app" / "src" / "locales"
_APP_SRC = _REPO / "app" / "src"

#: ``'some.key':`` at the start of an entry. Keys are always single
#: quoted in these files; values may use any of the three quote styles.
_KEY = re.compile(r"'([A-Za-z0-9_.\-]+)'\s*:")

_ENTRY = re.compile(
    r"""'([A-Za-z0-9_.\-]+)'\s*:\s*"""
    r"""(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"|`((?:[^`\\]|\\.)*)`)""",
    re.S,
)

#: ``t('some.key')`` with a literal. Keys built at runtime — ``t(`a.${b}`)``
#: — cannot be checked this way and are deliberately not attempted: a
#: guess about them would make this test lie rather than fail.
_CALL = re.compile(r"""\bt\(\s*['"]([A-Za-z0-9_.\-]+)['"]""")

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")

_LANGUAGES = ("en", "fr")


def _keys(language: str) -> list[str]:
    return _KEY.findall((_LOCALES / f"{language}.ts").read_text())


def _entries(language: str) -> dict[str, str]:
    text = (_LOCALES / f"{language}.ts").read_text()
    return {
        m.group(1): next(g for g in m.groups()[1:] if g is not None)
        for m in _ENTRY.finditer(text)
    }


def _sources():
    for path in sorted(_APP_SRC.rglob("*.ts*")):
        if "node_modules" in path.parts or path.parent == _LOCALES:
            continue
        yield path


@pytest.mark.parametrize("language", _LANGUAGES)
def test_the_parser_reads_every_entry(language):
    """If a value is ever written in a way the value pattern cannot read,
    the placeholder check below would skip it silently and pass for the
    wrong reason. Fail here instead, where the message is about parsing.
    """
    missed = set(_keys(language)) - set(_entries(language))
    assert missed == set(), f"{language}.ts: could not read values for {sorted(missed)}"


def test_every_key_the_ui_asks_for_is_defined():
    """A missing key does not throw and does not fall back to anything
    readable — the raw key is rendered into the page. This caught
    ``t('ai.save')`` on the AI Engine's primary button, which had been
    showing the literal text ``ai.save`` in both languages.
    """
    catalogue = set(_keys("en"))
    orphans: dict[str, list[str]] = {}
    for path in _sources():
        for key in _CALL.findall(path.read_text(errors="ignore")):
            if key not in catalogue:
                orphans.setdefault(key, []).append(
                    path.relative_to(_REPO).as_posix())
    assert orphans == {}, (
        "these keys are asked for by the UI and defined nowhere, so the "
        f"key itself is what renders: {orphans}")


def test_the_catalogues_define_the_same_keys():
    """French missing a key falls back to English silently: the page
    looks fine to whoever added it and is half-translated for everyone
    else. English missing a key that French defines is a deletion that
    only half happened."""
    en, fr = set(_keys("en")), set(_keys("fr"))
    assert sorted(en - fr) == [], "defined in English, missing in French"
    assert sorted(fr - en) == [], "defined in French, missing in English"


@pytest.mark.parametrize("language", _LANGUAGES)
def test_no_key_is_defined_twice(language):
    """These files hold many entries per line. A duplicate key is
    invisible on review and the later one silently wins."""
    seen = _keys(language)
    dupes = sorted({k for k in seen if seen.count(k) > 1})
    assert dupes == [], f"{language}.ts defines these twice: {dupes}"


def test_a_translation_never_drops_an_interpolation():
    """``'set by {{by}} at {{at}}'`` translated without ``{{at}}`` loses
    the timestamp with no error anywhere — the sentence just quietly says
    less than the English one."""
    en, fr = _entries("en"), _entries("fr")
    mismatched = {
        key: (sorted(_PLACEHOLDER.findall(value)),
              sorted(_PLACEHOLDER.findall(fr[key])))
        for key, value in en.items()
        if key in fr
        and sorted(_PLACEHOLDER.findall(value))
        != sorted(_PLACEHOLDER.findall(fr[key]))
    }
    assert mismatched == {}, (
        f"French drops or invents placeholders for: {mismatched}")
