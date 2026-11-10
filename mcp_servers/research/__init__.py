"""A narrow research gateway from an external MCP host to a Condor install.

The server lives in :mod:`mcp_servers.research.server` and is started with
``python -m mcp_servers.research.server``. This package stays import-light so
that ``-m`` does not import the server module twice.
"""
