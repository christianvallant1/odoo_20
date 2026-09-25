#!/usr/bin/env bash
# Set up a fresh Ubuntu 24.04 (or 26.04) machine to try Odoo 20 with the
# llm_assistant module and a local LLM served by Ollama.
#
# Run as root inside the VM:   sudo bash setup_odoo_vm.sh
#
# Settings (environment variables):
#   REPO_URL      git repository to run          (default: this fork)
#   REPO_BRANCH   branch to check out            (default: 20.0)
#   LLM_MODEL     Ollama model to download       (default: qwen3:4b)
#   SKIP_OLLAMA   1 to skip installing Ollama    (e.g. to use another LLM server)
#   NO_SYSTEMD    1 to skip the systemd services (containers without systemd)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/christianvallant1/odoo_20.git}"
REPO_BRANCH="${REPO_BRANCH:-20.0}"
LLM_MODEL="${LLM_MODEL:-qwen3:4b}"
SKIP_OLLAMA="${SKIP_OLLAMA:-0}"
NO_SYSTEMD="${NO_SYSTEMD:-0}"

ODOO_HOME=/opt/odoo
SRC="$ODOO_HOME/src"
VENV="$ODOO_HOME/venv"
CONF=/etc/odoo/odoo.conf
DB=odoo

step() { printf '\n==> %s\n' "$*"; }
as_odoo() { sudo -u odoo -H "$@"; }

if [[ $EUID -ne 0 ]]; then
    echo "Run this script as root: sudo bash $0" >&2
    exit 1
fi

step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git curl sudo postgresql python3-venv python3-dev build-essential \
    libpq-dev libldap2-dev libsasl2-dev libxml2-dev libxslt1-dev libjpeg-dev zlib1g-dev

step "Starting PostgreSQL"
if [[ "$NO_SYSTEMD" == 1 ]]; then
    pg_ctlcluster "$(ls /etc/postgresql | sort -V | tail -1)" main start || true
else
    systemctl enable --now postgresql
fi

step "Creating the odoo user and its database role"
id odoo &>/dev/null || useradd --system --create-home --home-dir "$ODOO_HOME" --shell /bin/bash odoo
mkdir -p "$ODOO_HOME" /etc/odoo /var/log/odoo
chown odoo:odoo "$ODOO_HOME" /var/log/odoo
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='odoo'" | grep -q 1 \
    || sudo -u postgres createuser --createdb odoo

step "Getting the code ($REPO_BRANCH)"
if [[ -d "$SRC/.git" ]]; then
    as_odoo git -C "$SRC" pull --ff-only
else
    as_odoo git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$SRC"
fi

step "Installing Python dependencies (a few minutes)"
supported() { "$1" -c 'import sys; sys.exit(not (3, 12) <= sys.version_info[:2] <= (3, 14))' 2>/dev/null; }
PYTHON=""
for candidate in python3 python3.12 python3.13 python3.14; do
    if command -v "$candidate" &>/dev/null && supported "$candidate"; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done
if [[ -z "$PYTHON" ]]; then
    echo "Odoo 20 needs Python 3.12 to 3.14; use Ubuntu 24.04 or newer." >&2
    exit 1
fi
if [[ -d "$VENV" ]] && ! supported "$VENV/bin/python"; then
    rm -rf "$VENV"
fi
[[ -d "$VENV" ]] || as_odoo "$PYTHON" -m venv "$VENV"
as_odoo "$VENV/bin/pip" install -q --upgrade pip wheel
as_odoo "$VENV/bin/pip" install -q -r "$SRC/requirements.txt"

step "Writing $CONF"
if [[ ! -f "$CONF" ]]; then
    cat > "$CONF" <<EOF
[options]
addons_path = $SRC/addons,$SRC/custom_addons
db_user = odoo
db_name = $DB
dbfilter = ^$DB\$
list_db = False
admin_passwd = $(openssl rand -hex 16)
http_port = 8069
max_cron_threads = 2
logfile = /var/log/odoo/odoo.log
EOF
    chown root:odoo "$CONF"
    chmod 640 "$CONF"
fi

if [[ "$SKIP_OLLAMA" != 1 ]]; then
    step "Installing Ollama and downloading $LLM_MODEL (a few GB)"
    command -v ollama &>/dev/null || curl -fsSL https://ollama.com/install.sh | sh
    [[ "$NO_SYSTEMD" == 1 ]] && (ollama serve &>/var/log/ollama.log &) && sleep 3
    ollama pull "$LLM_MODEL"
fi

step "Creating the database with demo data and the AI Assistant (a few minutes)"
if ! as_odoo psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB'" | grep -q 1; then
    as_odoo "$VENV/bin/python" "$SRC/odoo-bin" -c "$CONF" -d "$DB" -i contacts,llm_assistant \
        --with-demo --stop-after-init --logfile=
else
    # the database exists: bring the module up to date with the code
    as_odoo "$VENV/bin/python" "$SRC/odoo-bin" -c "$CONF" -d "$DB" -u llm_assistant --stop-after-init --logfile=
fi

step "Pointing the AI Assistant at Ollama"
as_odoo "$VENV/bin/python" "$SRC/odoo-bin" shell -c "$CONF" -d "$DB" --logfile= --no-http <<EOF
ICP = env['ir.config_parameter']
ICP.set_str('llm_assistant.base_url', 'http://localhost:11434/v1')
ICP.set_str('llm_assistant.model', '$LLM_MODEL')
ICP.set_int('llm_assistant.timeout', 300)  # small models on a CPU are slow
env.cr.commit()
EOF

if [[ "$NO_SYSTEMD" != 1 ]]; then
    step "Starting Odoo as a service"
    cat > /etc/systemd/system/odoo.service <<EOF
[Unit]
Description=Odoo
After=network.target postgresql.service

[Service]
User=odoo
ExecStart=$VENV/bin/python $SRC/odoo-bin -c $CONF
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now odoo
fi

IP="$(hostname -I | awk '{print $1}')"
cat <<EOF

Done. Open http://$IP:8069 and log in with admin / admin.
Then open AI Assistant > Chat and ask, for example, "Which contacts are in the United States?"

  Odoo log:        /var/log/odoo/odoo.log
  Restart Odoo:    sudo systemctl restart odoo
  Update the code: sudo -u odoo git -C $SRC pull && sudo systemctl restart odoo
EOF
