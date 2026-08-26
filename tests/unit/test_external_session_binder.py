from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.external_session_models import ExternalBinding
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from tests.fakes.external_session import make_binding


@pytest.fixture
def discovery() -> ExternalSessionDiscoveryService:
    return ExternalSessionDiscoveryService()


@pytest.fixture
def binding_store(tmp_path: Path) -> ExternalBindingStore:
    return ExternalBindingStore(data_dir=tmp_path)


@pytest.fixture
def binder(
    discovery: ExternalSessionDiscoveryService,
    binding_store: ExternalBindingStore,
    tmp_path: Path,
) -> ExternalSessionBinder:
    return ExternalSessionBinder(
        discovery=discovery,
        binding_store=binding_store,
        projects_dir=tmp_path / "projects",
    )


def _binding(session_id: str, user_id: int = 42) -> ExternalBinding:
    return make_binding(session_id=session_id, user_id=user_id)


def test_get_binding_returns_store_binding(
    binder: ExternalSessionBinder,
    binding_store: ExternalBindingStore,
) -> None:
    binding_store.save_binding(_binding("session-0001"))

    result = binder.get_binding("session-0001")

    assert result is not None
    assert result.session_id == "session-0001"
    assert result.user_id == 42


def test_get_binding_returns_none_for_unknown_session(binder: ExternalSessionBinder) -> None:
    assert binder.get_binding("unknown-session-0001") is None


def test_list_bound_returns_all_bindings_regardless_of_owner(
    binder: ExternalSessionBinder,
    binding_store: ExternalBindingStore,
) -> None:
    binding_store.save_binding(_binding("session-0001", user_id=42))
    binding_store.save_binding(_binding("session-0002", user_id=99))

    bindings = binder.list_bound()

    assert {b.session_id for b in bindings} == {"session-0001", "session-0002"}


def test_list_bound_empty_when_none(binder: ExternalSessionBinder) -> None:
    assert binder.list_bound() == []


def test_list_bound_for_user_filters_by_owner(
    binder: ExternalSessionBinder,
    binding_store: ExternalBindingStore,
) -> None:
    binding_store.save_binding(_binding("session-0001", user_id=42))
    binding_store.save_binding(_binding("session-0002", user_id=99))

    bindings = binder.list_bound_for_user(42)

    assert [b.session_id for b in bindings] == ["session-0001"]
