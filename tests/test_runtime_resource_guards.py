import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _compose_text() -> str:
    return (ROOT / "docker-compose.yml").read_text()


def test_database_pool_leaves_postgres_headroom_and_fails_fast() -> None:
    compose = _compose_text()

    expected_settings = {
        "DEFAULT_POOL_SIZE": 60,
        "RESERVE_POOL_SIZE": 5,
        "MAX_DB_CONNECTIONS": 65,
        "QUERY_WAIT_TIMEOUT": 15,
        "SERVER_IDLE_TIMEOUT": 60,
        "IDLE_TRANSACTION_TIMEOUT": 60,
    }
    actual: dict[str, int] = {}
    for name, expected in expected_settings.items():
        match = re.search(rf"^\s+{name}: [\"']?(\d+)[\"']?\s*$", compose, re.MULTILINE)
        assert match, f"{name} must be pinned in the pgbouncer service"
        actual[name] = int(match.group(1))
        assert actual[name] == expected

    assert actual["MAX_DB_CONNECTIONS"] <= 65
    assert actual["DEFAULT_POOL_SIZE"] + actual["RESERVE_POOL_SIZE"] <= actual["MAX_DB_CONNECTIONS"]


def test_postgres_and_api_process_have_resource_safety_limits() -> None:
    compose = _compose_text()
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "idle_in_transaction_session_timeout=60000" in compose
    assert re.search(r"nofile:\s*\n\s+soft: 65535\s*\n\s+hard: 65535", compose)
    assert "--limit-concurrency 200" in dockerfile
    assert "--backlog 512" in dockerfile


def test_health_check_does_not_depend_on_the_worker_thread_pool() -> None:
    app_tree = ast.parse((ROOT / "src" / "app.py").read_text())
    health_functions = [
        node
        for node in ast.walk(app_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "health_check"
    ]

    assert len(health_functions) == 1
    assert isinstance(health_functions[0], ast.AsyncFunctionDef)
