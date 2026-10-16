"""
tests/test_server.py
Fail-Closed tests verifying PEP 562 lazy loading in mlit-geospatial-mcp server.py.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, patch

# Ensure src is in sys.path
sys.path.insert(0, "src")
import server


class TestServerLazyImports(unittest.TestCase):
    def test_lazy_import_not_loaded_on_initial_import(self):
        """
        Verify that importing server in a clean subprocess does not load
        request_processor.handler or any of the 30 MLIT API classes.
        """
        code = """
import sys
sys.path.insert(0, "src")
import server

# Verify request_processor.handler is NOT loaded into sys.modules
assert "request_processor.handler" not in sys.modules, (
    "request_processor.handler was unexpectedly loaded on initial import of server!"
)
assert "request_processor" not in sys.modules, (
    "request_processor package was unexpectedly loaded on initial import of server!"
)
sys.exit(0)
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"Subprocess failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}",
        )

    def test_handle_request_resolves_via_getattr(self):
        """
        Verify accessing server.handle_request dynamically imports and caches the handler.
        """
        handler = getattr(server, "handle_request")
        self.assertTrue(callable(handler))
        self.assertIn("request_processor.handler", sys.modules)
        # Verify it's cached in module globals
        self.assertIs(server.handle_request, handler)

    def test_unknown_attribute_raises_attribute_error(self):
        """
        Verify accessing non-existent attributes on server raises AttributeError.
        """
        with self.assertRaises(AttributeError):
            _ = getattr(server, "non_existent_symbol_xyz")

    def test_mock_patch_handle_request(self):
        """
        Verify that unittest.mock.patch works seamlessly on server.handle_request.
        """
        with patch("server.handle_request", new_callable=AsyncMock) as mock_hr:
            self.assertIs(server.handle_request, mock_hr)
            mock_hr.return_value = {"status": "mocked"}


if __name__ == "__main__":
    unittest.main()
