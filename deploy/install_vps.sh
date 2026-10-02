#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 HOSTNAME" >&2
    exit 2
fi

app_hostname="$1"
app_dir="/opt/pdf-text-editor"
app_user="pdfeditor"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv libfreetype6 nginx certbot python3-certbot-nginx

if ! id "$app_user" >/dev/null 2>&1; then
    useradd --system --home-dir "$app_dir" --shell /usr/sbin/nologin "$app_user"
fi

python3 -m venv "$app_dir/.venv"
"$app_dir/.venv/bin/python" -m pip install --upgrade pip
"$app_dir/.venv/bin/pip" install -r "$app_dir/requirements.txt"

chown -R root:root "$app_dir"
chmod -R a+rX "$app_dir"

install -m 0644 "$app_dir/deploy/pdf-editor.service" /etc/systemd/system/pdf-editor.service
sed "s/__HOSTNAME__/$app_hostname/g" "$app_dir/deploy/nginx.conf" \
    > /etc/nginx/sites-available/pdf-editor
ln -sfn /etc/nginx/sites-available/pdf-editor /etc/nginx/sites-enabled/pdf-editor
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl daemon-reload
systemctl enable --now pdf-editor.service
systemctl enable --now nginx.service
systemctl reload nginx.service

curl --fail --silent http://127.0.0.1:8000/health
echo
