import os
import sqlite3
from flask import Flask, render_template_string, jsonify

app = Flask(__name__)

# This helps the app work correctly on Render's servers
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Simple HTML to test if the connection works
TEST_HTML = """
<!DOCTYPE html>
<html>
<head><title>Test</title></head>
<body>
    <h1>Ishlayapti!</h1>
    <p id="status">Ulanmoqda...</p>
    <script>
        fetch('/api/test')
            .then(res => res.json())
            .then(data => document.getElementById('status').innerText = data.message)
            .catch(err => document.getElementById('status').innerText = "Xato!");
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    return "Bot is running!"

@app.route('/webapp')
def webapp():
    return render_template_string(TEST_HTML)

@app.route('/api/test')
def api_test():
    return jsonify({"message": "Aloqa muvaffaqiyatli!"})

if __name__ == '__main__':
    # Render provides the port automatically
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
