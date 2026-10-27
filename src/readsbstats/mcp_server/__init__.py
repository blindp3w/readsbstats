"""readsbstats MCP server (opt-in, ADR-0022).

A separate process that exposes the local web API to LLM clients over MCP.
Rule: nothing in this package imports ``readsbstats.database`` or
``readsbstats.api`` (the unit also hides the database from the process).
"""
