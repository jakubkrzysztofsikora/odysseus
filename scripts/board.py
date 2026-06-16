#!/usr/bin/env python3
import json
import sys
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('add', nargs='?', default='add')
parser.add_argument('--type', default='incident')
parser.add_argument('--service', default='unknown')
parser.add_argument('--title', default='Untitled')
parser.add_argument('--priority', default='medium')
parser.add_argument('--nick', default='unknown')
parser.add_argument('--note', default='')

args = parser.parse_args()
task_id = f"TASK-{args.service.upper()}-{abs(hash(args.title)) % 10000:04d}"

result = {
    "id": task_id,
    "type": args.type,
    "service": args.service,
    "title": args.title,
    "priority": args.priority,
    "nick": args.nick,
    "note": args.note,
    "status": "created"
}

print(json.dumps(result))
