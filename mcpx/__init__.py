"""
MCP protocol integration for Crannia.

- Bridge external MCP server tools into bus/registry
- Host built-in MCP servers (web_search, etc.)
- Manage MCP server lifecycle

Architecture:
  LLM Function Calling → bus/registry (unified tool view)
    ├─ Native Skills (file_parser, office_parser, ...)
    └─ MCP Bridge
         ├─ Built-in MCP Servers (web_search, ...)
         └─ External MCP Clients (stdio / HTTP)
"""
