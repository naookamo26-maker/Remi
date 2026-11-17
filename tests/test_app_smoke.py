from remi.ui.main_window import MainWindow


def test_main_window_hides_instead_of_closing(qapp):
    w = MainWindow()
    w.show()
    w.close()
    assert not w.isVisible()
    assert w.allow_close is False
