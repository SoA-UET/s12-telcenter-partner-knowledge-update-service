from . import app
import os

# Get host and port from environment
host = os.getenv('FLASK_HOST', '0.0.0.0')
port = int(os.getenv('FLASK_PORT', 5000))

if __name__ == "__main__":
    app.run(host=host, port=port, debug=True)
