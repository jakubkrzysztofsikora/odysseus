#!/usr/bin/env python3
"""
Security Validator Plugin for Claude Code
This plugin validates tool usage and enforces security policies
"""

import json
import os
import re
from typing import Dict, Any

class SecurityValidator:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.blocked_patterns = config.get("blocked_commands", [])
        self.allowed_dirs = config.get("allowed_directories", [])

    def validate_command(self, tool_name: str, tool_input: str) -> bool:
        """Validate if a tool command is safe to execute"""

        # Check for blocked patterns
        for pattern in self.blocked_patterns:
            if re.search(pattern, tool_input):
                return False

        # Check if working in allowed directories
        if tool_name in ["bash", "shell", "execute"]:
            for allowed_dir in self.allowed_dirs:
                if allowed_dir in tool_input:
                    return True
            return False

        return True

    def log_tool_usage(self, tool_name: str, tool_input: str, result: str) -> None:
        """Log tool usage for auditing"""
        log_entry = {
            "timestamp": "2024-01-15T10:30:00Z",
            "tool": tool_name,
            "input": tool_input,
            "result": result,
            "status": "completed"
        }

        # In real implementation, this would write to a log file
        print(f"LOG: {json.dumps(log_entry, indent=2)}")

if __name__ == "__main__":
    # Example usage
    config = {
        "blocked_commands": ["rm -rf", "chmod 777"],
        "allowed_directories": ["/tmp", "/home/user"]
    }

    validator = SecurityValidator(config)

    # Test validation
    test_commands = [
        ("bash", "ls -la /tmp", "Should be allowed"),
        ("bash", "rm -rf /", "Should be blocked"),
        ("bash", "cd /home/user && ls", "Should be allowed")
    ]

    for tool, cmd, expected in test_commands:
        is_valid = validator.validate_command(tool, cmd)
        print(f"Command: {cmd}")
        print(f"Valid: {is_valid} - {expected}")
        print("---")
