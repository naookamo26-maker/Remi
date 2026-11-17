import pytest

from remi.ui.theme import CONTRAST_PAIRS, DARK, LIGHT, ThemeManager, contrast_ratio


@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
@pytest.mark.parametrize("fg,bg", CONTRAST_PAIRS)
def test_contrast_is_at_least_aa(palette, fg, bg):
    d = palette.as_dict()
    ratio = contrast_ratio(d[fg], d[bg])
    assert ratio >= 4.5, f"{palette.name}: {fg} on {bg} = {ratio:.2f}"


def test_palettes_have_same_tokens():
    assert LIGHT.as_dict().keys() == DARK.as_dict().keys()


def test_theme_manager_switches(qapp):
    tm = ThemeManager(qapp, "light")
    seen = []
    tm.themeChanged.connect(seen.append)
    assert tm.palette is LIGHT
    tm.set_mode("dark")
    assert tm.palette is DARK
    assert seen == [DARK]
    assert qapp.palette().base().color().name().upper() == DARK.window
    tm.set_mode("light")
    assert tm.palette is LIGHT


def test_invalid_mode(qapp):
    tm = ThemeManager(qapp, "light")
    with pytest.raises(ValueError):
        tm.set_mode("pink")
