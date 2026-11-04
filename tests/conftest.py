"""Default suite cannot collect demo tests that connect to SessionLocal."""
import os
import pytest
from pathlib import Path

DEMO_FILES = {'test_multitenant.py', 'test_sync_offline.py'}


def pytest_addoption(parser):
    parser.addoption('--run-demo-integration', action='store_true', default=False,
                     help='Collect legacy integration tests against an explicitly configured, seeded test database.')


def pytest_configure(config):
    if config.getoption('--run-demo-integration'):
        url = os.environ.get('DEMO_TEST_DATABASE_URL', '')
        from sqlalchemy.engine import make_url
        try:
            database = make_url(url).database or ''
        except Exception:
            raise pytest.UsageError('DEMO_TEST_DATABASE_URL must name a disposable test database.')
        if not (database.endswith('_test') or Path(database).name.endswith('_test.db')):
            raise pytest.UsageError('Legacy integration database must end in _test or _test.db, never the working database.')
        os.environ['DATABASE_URL'] = url


def pytest_ignore_collect(collection_path, config):
    if collection_path.name in DEMO_FILES and not config.getoption('--run-demo-integration'):
        return True
    return None
