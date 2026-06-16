#!/usr/bin/env python3
"""
Classified Service - Team 1
Vulnerable Flask application for CTF testing
"""
from flask import Flask, request, jsonify, render_template_string
import subprocess
import os
import sqlite3

app = Flask(__name__)

# Vulnerability 1: SSTI (Server Side Template Injection)
@app.route('/template')
def template_vuln():
    name = request.args.get('name', 'Guest')
    template = f"<h1>Hello {name}!</h1>"
    return render_template_string(template)

# Vulnerability 2: Command Injection
@app.route('/exec')
def exec_vuln():
    cmd = request.args.get('cmd', 'echo hello')
    try:
        result = subprocess.check_output(cmd, shell=True, text=True)
        return f"Command output: {result}"
    except Exception as e:
        return f"Error: {str(e)}"

# Vulnerability 3: SQL Injection
@app.route('/search')
def search_vuln():
    user_id = request.args.get('id', '1')
    conn = sqlite3.connect('/tmp/test.db')
    cursor = conn.cursor()
    query = f"SELECT * FROM users WHERE id = {user_id}"
    try:
        cursor.execute(query)
        results = cursor.fetchall()
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'error': str(e)})

# Vulnerability 4: Hardcoded flag (easiest)
FLAG = "1_1_1_a1b2c3d4e5f6g7h8"

@app.route('/debug')
def debug_vuln():
    return f"Debug info: {FLAG}"

@app.route('/')
def index():
    return "Classified Service - Team 1"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=443, ssl_context='adhoc')
