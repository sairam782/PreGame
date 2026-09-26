import mongomock
import pytest


@pytest.fixture
def db():
    """A fresh in-memory database per test (no network, no transactions)."""
    client = mongomock.MongoClient(tz_aware=True)
    return client["pregame_test"]
