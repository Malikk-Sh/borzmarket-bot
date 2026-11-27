"""Общий сброс кэшей сетевого слоя. Нужен и pytest (conftest), и обычному unittest."""


def reset_state() -> None:
    import net
    import prices
    import wb
    import wbcon
    wbcon._cache.clear()
    prices._RESULT_CACHE.clear()
    wb._DETAIL_CACHE.clear()
    wb._basket_cache.clear()
    net._inflight.clear()


class CleanState:
    """Подмешивается в TestCase: каждый тест начинает с пустых кэшей."""

    def setUp(self):
        reset_state()
        super().setUp()
