"""Development entry point.

For a real deployment run this through a WSGI server instead:

    waitress-serve --port=8080 app:app          # Windows
    gunicorn -w 4 -b 0.0.0.0:8080 app:app       # Linux

Host, port and debug mode come from the environment (see .env.example) so the
same code runs unchanged on any machine.
"""

import os

from app import create_app

app = create_app()

if __name__ == '__main__':
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '5000'))
    debug = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'

    print(f'  {app.config["APP_NAME"]}')
    print(f'  Local:   http://127.0.0.1:{port}')

    if not app.config.get('CLIENT_ID') or 'PASTE_' in str(
        app.config.get('CLIENT_SECRET', '')
    ):
        print('  Warning: Microsoft Entra ID is not configured in .env.')

    app.run(host=host, port=port, debug=debug)