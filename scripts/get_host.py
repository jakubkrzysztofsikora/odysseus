#!/usr/bin/env python3
"""Mock get_host.py for testing the logs skill"""
import sys
import json

# Mock service to IP mapping for testing
SERVICE_MAP = {
    'classified': ('172.28.0.10', 'keys/id_team'),
    'docs': ('172.28.0.11', 'keys/id_team'),
    'milstorage': ('172.28.0.12', 'keys/id_team'),
    'gitspace': ('172.28.0.13', 'keys/id_team'),
    'privatechat': ('172.28.0.14', 'keys/id_team'),
    'atak': ('172.28.0.15', 'keys/id_team'),
    'milnet': ('172.28.0.16', 'keys/id_team'),
    'mock_up': ('172.28.0.17', 'keys/id_team'),
    'secrets': ('172.28.0.18', 'keys/id_team'),
}

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: get_host.py <service>")
        sys.exit(1)

    service = sys.argv[1]
    if service in SERVICE_MAP:
        ip, key = SERVICE_MAP[service]
        print(f"{ip} {key}")
    else:
        print("172.28.0.1 keys/id_team")
