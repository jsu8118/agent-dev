"""labkit.mock - an offline, rule-validating stand-in for the Claude Messages API.

Lab scripts never import this directly; `labkit.get_client()` wires it in when no API
key is configured.  Scenario authors use the builders re-exported here.
"""

from .api import MockAnthropicAPI, get_mock_api
from .errors import ApiError
from .registry import dispatch, registered, scenario
from .reply import (Reply, bash, cite, cited_text, create_file, json_reply, refuse, run_code, say, search_tools, tool,
                    use_tools, view_file)
from .request import MockRequest, ToolCall

__all__ = [
    "MockAnthropicAPI", "get_mock_api", "ApiError", "dispatch", "registered", "scenario",
    "Reply", "cite", "cited_text", "json_reply", "refuse", "say", "tool", "use_tools", "MockRequest", "ToolCall",
    "search_tools", "run_code", "bash", "create_file", "view_file",
]
