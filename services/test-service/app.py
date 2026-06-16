from flask import Flask, jsonify
import os

app = Flask(__name__)

# Configuration - this will have an error
CONFIG = {
    "port": int(os.getenv("PORT", "5000")),
    "debug": os.getenv("DEBUG", "false").lower() == "true",
    "database_url": os.getenv("DATABASE_URL", "sqlite:///app.db")
}

@app.route('/health')
def health():
    # This will fail if DATABASE_URL is not set correctly
    if not CONFIG["database_url"].startswith("sqlite://"):
        raise ValueError(f"Invalid database URL: {CONFIG['database_url']}")
    return jsonify({"status": "healthy", "config": CONFIG})

@app.route('/')
def index():
    return jsonify({"message": "Test Service is running", "config": CONFIG})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=CONFIG["port"])
