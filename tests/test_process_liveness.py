"""Unit tests for the ``app.services.process_liveness`` module.

Validates: Requirements 10.4
"""

import app.services.process_liveness as mod


def test_module_docstring_documents_local_socket_assumption() -> None:
    """The module docstring documents the Local-Socket Assumption.

    Per Requirement 10.4, the ``Local_Socket_Assumption`` must be co-located
    with the ``process_is_alive`` probe (the module-level docstring), so that a
    future remote hook socket is recognized as a trigger to disable pid
    liveness. The module spells it in all-caps as ``LOCAL_SOCKET_ASSUMPTION``.

    Validates: Requirements 10.4
    """
    assert mod.__doc__ is not None
    assert "LOCAL_SOCKET_ASSUMPTION" in mod.__doc__
