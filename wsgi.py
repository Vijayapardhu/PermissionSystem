"""WSGI entry point.

The WSGI servers address this file as module:attribute, so the filename must
not collide with the `app` package next to it. A package directory always wins
over a same-named .py file in Python's import system, which is why an `app.py`
here would be unreachable as `app:app` and gunicorn would abort with
"Failed to find attribute 'app' in 'app'".

For a real deployment run this through a WSGI server instead:

    waitress-serve --port=8080 wsgi:app          # Windows
    gunicorn -w 4 -b 0.0.0.0:8080 wsgi:app       # Linux

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