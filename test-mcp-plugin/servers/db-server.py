#!/usr/bin/env python3
"""
Simple MCP server for database operations
Demonstrates MCP integration for Claude Code plugins
"""
import json
import sys
import os
from typing import Dict, Any

class DatabaseMCPServer:
    def __init__(self):
        self.tools = {
            "query_database": {
                "name": "query_database",
                "description": "Execute a SQL query against the connected database",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "SQL query to execute"},
                        "params": {"type": "array", "items": {"type": "string"}, "description": "Query parameters"}
                    },
                    "required": ["query"]
                }
            },
            "list_tables": {
                "name": "list_tables",
                "description": "List all tables in the connected database",
                "inputSchema": {"type": "object", "properties": {}}
            },
            "get_table_schema": {
                "name": "get_table_schema",
                "description": "Get schema information for a specific table",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "table_name": {"type": "string", "description": "Name of the table"}
                    },
                    "required": ["table_name"]
                }
            }
        }

    def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle incoming MCP requests"""
        method = request.get("method")

        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": name,
                        "description": tool["description"],
                        "inputSchema": tool["inputSchema"]
                    }
                    for name, tool in self.tools.items()
                ]
            }

        elif method == "tools/call":
            tool_name = request.get("params", {}).get("name")
            arguments = request.get("params", {}).get("arguments", {})

            if tool_name in self.tools:
                return self._execute_tool(tool_name, arguments)
            else:
                return {"error": f"Tool {tool_name} not found"}

        return {"error": f"Unknown method: {method}"}

    def _execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a specific tool"""
        if tool_name == "query_database":
            query = arguments.get("query", "")
            params = arguments.get("params", [])
            # Simulate database query
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Executed query: {query}\nWith parameters: {params}\nReturned 42 rows"
                    }
                ]
            }

        elif tool_name == "list_tables":
            # Simulate listing tables
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Available tables:\n- users\n- products\n- orders\n- customers"
                    }
                ]
            }

        elif tool_name == "get_table_schema":
            table_name = arguments.get("table_name", "")
            # Simulate schema retrieval
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Schema for table '{table_name}':\nColumns: id (INT), name (VARCHAR), created_at (TIMESTAMP)"
                    }
                ]
            }

        return {"error": f"Unknown tool: {tool_name}"}

def main():
    server = DatabaseMCPServer()

    # Read from stdin, write to stdout (stdio mode)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
            response = server.handle_request(request)
            print(json.dumps(response))
            sys.stdout.flush()
        except json.JSONDecodeError:
            print(json.dumps({"error": "Invalid JSON input"}))
            sys.stdout.flush()
        except Exception as e:
            print(json.dumps({"error": str(e)}))
            sys.stdout.flush()

if __name__ == "__main__":
    main()
