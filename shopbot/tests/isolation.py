"""Общий сброс кэшей сетевого слоя. Нужен и pytest (conftest), и обычному unittest."""


def reset_state() -> None:
    import net
    import prices
    import state
    import wb
    import wbcon
    state.reset()
    for window in wbcon._recent.values():
        window.clear()
    for market in wbcon._blocked_until:
        wbcon._blocked_until[market] = 0.0
    wbcon._cache.clear()
    prices._RESULT_CACHE.clear()
    wb._DETAIL_CACHE.clear()
    wb._basket_cache.clear()
    wb._endpoint_order[:] = list(wb.DETAIL_ENDPOINTS)
    net._inflight.clear()


class CleanState:
    """Подмешивается в TestCase: каждый тест начинает с пустых кэшей."""

    def setUp(self):
        reset_state()
        super().setUp()
