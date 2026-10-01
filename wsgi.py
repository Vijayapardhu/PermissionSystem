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
import zlib

from app import create_app

flask_app = create_app()


class GzipMiddleware:
    """Compress text responses for clients that advertise support.

    Gunicorn ships no compressor and Flask has none, so every page went out
    uncompressed: the stylesheet alone is about 39 KB, sent again on every
    navigation. The transfer log confirmed the cost -- the same stylesheet was
    re-fetched on each page view.

    Deliberately conservative:
      * text-ish content types only, so uploaded PNG and PDF proofs are never
        touched. They are already compressed, and gzip only inflates them;
      * a small size floor, because the gzip framing can exceed the original on
        a very short body;
      * only when the request actually sent Accept-Encoding with a non-zero q;
      * never on 204/304, which must not carry a body or a Content-Length.

    The body is buffered rather than streamed so the decision can be made after
    the response headers are known -- the content type is what decides whether
    compressing is worthwhile. Every response this project returns is a page, a
    JSON document or a static file, so buffering costs nothing in practice.
    """

    MIN_BYTES = 1024
    COMPRESSIBLE = (
        'text/', 'application/javascript', 'application/json',
        'application/xml', 'image/svg+xml',
    )
    # Replaced rather than appended, so the framing always matches the body.
    REPLACED = frozenset({'content-length', 'content-encoding'})

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    @staticmethod
    def _accepts_gzip(environ) -> bool:
        header = environ.get('HTTP_ACCEPT_ENCODING', '').lower()
        if 'gzip' not in header:
            return False
        # "gzip;q=0" is an explicit refusal; plain "gzip" or "gzip;q=1" is not.
        return 'gzip;q=0' not in header.replace(' ', '')

    @staticmethod
    def _is_compressible(status: str, headers, body: bytes) -> bool:
        code = status.split(' ', 1)[0]
        if code in ('204', '304'):
            return False
        if len(body) < GzipMiddleware.MIN_BYTES:
            return False
        for key, value in headers:
            if key.lower() == 'content-type':
                return value.split(';', 1)[0].lower().startswith(
                    GzipMiddleware.COMPRESSIBLE
                )
        return False

    def __call__(self, environ, start_response):
        if not self._accepts_gzip(environ):
            return self.wsgi_app(environ, start_response)

        captured = {}

        def capture(status, headers, exc_info=None):
            # Hold the headers back; the body has to be read before it is known
            # whether compression applies. The no-op write callable keeps any
            # app that streams from raising.
            captured['status'] = status
            captured['headers'] = headers
            captured['exc_info'] = exc_info
            return lambda data: None

        body = b''.join(self.wsgi_app(environ, capture))

        status = captured.get('status', '500 Internal Server Error')
        headers = list(captured.get('headers', []))
        exc_info = captured.get('exc_info')

        if self._is_compressible(status, headers, body):
            # zlib.compress emits a zlib wrapper; WSGI's Content-Encoding: gzip
            # requires a gzip container, so wbits=16+MAX_WBITS is the gzip one.
            body = zlib.compress(body, 6, zlib.MAX_WBITS | 16)
            headers = [(k, v) for k, v in headers if k.lower() not in self.REPLACED]
            headers.append(('Content-Encoding', 'gzip'))

        code = status.split(' ', 1)[0]
        if code not in ('204', '304'):
            headers = [(k, v) for k, v in headers if k.lower() != 'content-length']
            headers.append(('Content-Length', str(len(body))))

        start_response(status, headers, exc_info)
        return [body]


# What gunicorn and waitress load. The Flask app is kept separate so the
# development server below can still use its reloader and debugger.
app = GzipMiddleware(flask_app)


if __name__ == '__main__':
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '5000'))
    debug = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'

    print(f'  {flask_app.config["APP_NAME"]}')
    print(f'  Local:   http://{host}:{port}')

    if not flask_app.config.get('CLIENT_ID') or 'PASTE_' in str(
        flask_app.config.get('CLIENT_SECRET', '')
    ):
        print('  Warning: Microsoft Entra ID is not configured in .env.')

    flask_app.run(host=host, port=port, debug=debug)
