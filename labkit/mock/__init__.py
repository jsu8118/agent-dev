"""labkit.mock - an offline, rule-validating stand-in for the Claude Messages API.

Lab scripts never import this directly; `labkit.get_client()` wires it in when no API
key is configured.  Scenario authors use the builders re-exported here.
"""

from .api import MockAnthropicAPI, get_mock_api
from .errors import ApiError
from .registry import dispatch, registered, scenario
from .reply import Reply, json_reply, refuse, say, tool, use_tools
from .request import MockRequest, ToolCall

__all__ = [
    "MockAnthropicAPI", "get_mock_api", "ApiError", "dispatch", "registered", "scenario",
    "Reply", "json_reply", "refuse", "say", "tool", "use_tools", "MockRequest", "ToolCall",
]
