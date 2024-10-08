import pytest
import asyncio
from microrabbit import Client
from pytest_asyncio import fixture, is_async_test
from typing import AsyncIterator


@fixture(scope="function")
async def client() -> AsyncIterator[Client]:
    async with Client(host="amqp://guest:guest@localhost/") as c:
        yield c

def pytest_collection_modifyitems(items):
    pytest_asyncio_tests = (item for item in items if is_async_test(item))
    session_scope_marker = pytest.mark.asyncio(loop_scope="function")
    for async_test in pytest_asyncio_tests:
        async_test.add_marker(session_scope_marker, append=False)
