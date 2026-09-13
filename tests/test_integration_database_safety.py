"""Safety checks for the disposable integration database guard."""

import pytest

from tests.conftest import _validated_test_database_name


def test_destructive_integration_setup_accepts_only_the_test_database_name() -> None:
    assert _validated_test_database_name(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/agent_platform_test"
    ) == "agent_platform_test"

    with pytest.raises(RuntimeError, match="Refusing destructive integration setup"):
        _validated_test_database_name(
            "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_platform"
        )
