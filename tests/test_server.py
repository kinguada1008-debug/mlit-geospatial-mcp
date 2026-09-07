"""Unit tests for MLIT Geospatial MCP Server and Request Handlers.

Tests cover:
- MCP Server tool listing (handle_list_tools)
- Tool payload construction and parameter filtering (build_payload)
- Tool invocation and TextContent response format (handle_call_tool)
- Error handling for invalid/missing arguments
"""

import json
from unittest.mock import AsyncMock, patch
import pytest

from server import handle_call_tool, handle_list_tools, server
from tools import API_SPECS, TOOLS
from tools.multi_api import API_SPEC as MULTI_API_SPEC
from utils.payload import build_payload


@pytest.mark.anyio
async def test_handle_list_tools():
    """Verify handle_list_tools returns get_multi_api tool with valid schema."""
    tools = await handle_list_tools()
    assert len(tools) >= 1
    tool_names = [t.name for t in tools]
    assert "get_multi_api" in tool_names

    multi_tool = next(t for t in tools if t.name == "get_multi_api")
    assert "target_api" in multi_tool.description
    assert multi_tool.inputSchema is not None
    assert "properties" in multi_tool.inputSchema
    assert "lat" in multi_tool.inputSchema["properties"]
    assert "lon" in multi_tool.inputSchema["properties"]


def test_build_payload_valid_arguments():
    """Verify build_payload extracts coordinates and allowed parameters."""
    args = {
        "lat": "35.6812",
        "lon": "139.7671",
        "target_apis": [1, 3],
        "distance": 500,
        "year": "2024",
        "unsupported_extra_param": "should_be_ignored",
    }

    payload = build_payload(spec=MULTI_API_SPEC, args=args)

    assert len(payload["coordinates"]) == 1
    assert payload["coordinates"][0]["lat"] == 35.6812
    assert payload["coordinates"][0]["lon"] == 139.7671
    assert payload["target_apis"] == [1, 3]
    assert payload["distance"] == 500
    assert payload["year"] == "2024"
    assert "unsupported_extra_param" not in payload


def test_build_payload_missing_coordinates():
    """Verify build_payload raises KeyError when required lat/lon are missing."""
    with pytest.raises(KeyError):
        build_payload(spec=MULTI_API_SPEC, args={"target_apis": [1]})


@pytest.mark.anyio
async def test_handle_call_tool_success():
    """Verify handle_call_tool processes arguments and returns formatted TextContent."""
    dummy_result = {
        "status": "success",
        "results": [
            {"api_id": 1, "data": [{"price": 50000000, "station": "Tokyo"}]}
        ],
        "map_url": "https://example.com/map"
    }

    with patch("server.handle_request", new_callable=AsyncMock) as mock_handle:
        mock_handle.return_value = dummy_result

        arguments = {
            "lat": "35.6812",
            "lon": "139.7671",
            "target_apis": [1],
            "save_file": False,
        }

        response = await handle_call_tool("get_multi_api", arguments)

        assert len(response) == 1
        assert response[0].type == "text"
        parsed = json.loads(response[0].text)
        assert parsed["status"] == "success"
        assert len(parsed["results"]) == 1
        assert parsed["results"][0]["api_id"] == 1

        mock_handle.assert_awaited_once()


@pytest.mark.anyio
async def test_handle_call_tool_unknown_tool():
    """Verify handle_call_tool raises KeyError on unregistered tool name."""
    with pytest.raises(KeyError):
        await handle_call_tool("unknown_tool_name", {"lat": "35.0", "lon": "139.0"})


def test_build_payload_invalid_coordinate_types():
    """Verify build_payload raises ValueError when lat/lon are not convertible to float."""
    with pytest.raises(ValueError):
        build_payload(spec=MULTI_API_SPEC, args={"lat": "not_a_number", "lon": "139.7671"})


def test_tool_definitions_consistency():
    """Verify all TOOLS have matching API_SPECS and adhere to valid MCP schema definitions."""
    assert len(TOOLS) > 0
    for tool in TOOLS:
        assert tool.name in API_SPECS
        spec = API_SPECS[tool.name]
        assert spec.tool_name == tool.name
        assert tool.description is not None and len(tool.description) > 0
        assert tool.inputSchema is not None
        assert tool.inputSchema.get("type") == "object"
        assert "properties" in tool.inputSchema

