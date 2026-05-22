import typer

from teams_interaction.cli import _AUTHOR_COLORS, _get_color_for_author


def test_get_color_for_author_is_consistent():
    author = "Alice"
    color1 = _get_color_for_author(author)
    color2 = _get_color_for_author(author)
    assert color1 == color2
    assert color1 in _AUTHOR_COLORS


def test_different_authors_get_different_colors():
    # Note: with only 12 colors, there might be collisions,
    # but for these names they should be different.
    color_alice = _get_color_for_author("Alice")
    color_bob = _get_color_for_author("Bob")
    assert color_alice != color_bob


def test_empty_author_gets_white():
    assert _get_color_for_author("") == typer.colors.WHITE
    assert _get_color_for_author(None) == typer.colors.WHITE
