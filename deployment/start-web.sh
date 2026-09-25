#!/bin/bash
set -euo pipefail

if [ ! -s /Kiwi/ssl/localhost.crt ] || [ ! -s /Kiwi/ssl/localhost.key ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
        -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
        -keyout /Kiwi/ssl/localhost.key -out /Kiwi/ssl/localhost.crt
    chmod 600 /Kiwi/ssl/localhost.key
fi
nginx -c /Kiwi/deployment/nginx.conf
exec uwsgi --ini /Kiwi/etc/uwsgi.conf --processes 2 --die-on-term
