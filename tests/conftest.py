import pytest
from src.utils.config import settings


@pytest.fixture
def config():
    return settings()
