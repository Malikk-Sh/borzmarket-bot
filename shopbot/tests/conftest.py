import pytest

from isolation import reset_state


@pytest.fixture(autouse=True)
def clean_network_state():
    """Кэши сетевого слоя общие на процесс. Каждый тест начинает с пустых."""
    reset_state()
    yield
