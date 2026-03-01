"""Tests for the URL-to-path conversion logic."""

from pathlib import Path

from mbi_crawler.output.writer import url_to_path


def test_root_becomes_index() -> None:
    assert url_to_path("https://www.oeaw.ac.at/mbi", "https://www.oeaw.ac.at/mbi") == Path(
        "index.md"
    )


def test_trailing_slash_index() -> None:
    assert url_to_path(
        "https://www.oeaw.ac.at/mbi/research/", "https://www.oeaw.ac.at/mbi"
    ) == Path("research/index.md")


def test_deep_path() -> None:
    result = url_to_path(
        "https://www.oeaw.ac.at/mbi/research/projects",
        "https://www.oeaw.ac.at/mbi",
    )
    assert result == Path("research/projects.md")


def test_twiki_page() -> None:
    result = url_to_path(
        "https://twiki.cern.ch/twiki/bin/view/CMSPublic/WorkBookCMSCollisions",
        "https://twiki.cern.ch/twiki/bin/view/CMSPublic/WorkBook",
    )
    assert result.suffix == ".md"


def test_sanitize_special_chars() -> None:
    result = url_to_path(
        "https://www.oeaw.ac.at/mbi/page with spaces/",
        "https://www.oeaw.ac.at/mbi",
    )
    assert " " not in str(result)
