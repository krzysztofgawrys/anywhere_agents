#!/bin/sh
# Ensure the running UID has a passwd entry so bash shows a proper prompt
# and git/ssh don't complain about unknown user.
if ! whoami >/dev/null 2>&1; then
    echo "app:x:$(id -u):$(id -g):app:/home/app:/bin/bash" >> /etc/passwd
    echo "app:x:$(id -g):" >> /etc/group 2>/dev/null || true
fi
exec "$@"
