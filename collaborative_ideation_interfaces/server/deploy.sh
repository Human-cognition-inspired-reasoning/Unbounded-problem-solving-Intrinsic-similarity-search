#!/usr/bin/env bash
#
# VM 배포. git pull -> 빌드 -> 재시작.
#
#   sudo /opt/co-scientist/app/server/deploy.sh
#
# 경로는 환경변수로 덮어쓸 수 있다. 기본값은 배포 런북의 배치를 따른다.

set -euo pipefail
umask 077

APP="${APP:-/opt/co-scientist/app}"
VENV="${VENV:-/opt/co-scientist/venv}"
DATA_DIR="${DATA_DIR:-/var/lib/co-scientist/data}"
SERVICE="${SERVICE:-coscientist}"
RUN_AS="${RUN_AS:-coscientist}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:5000/api/health}"

AS=(sudo -u "$RUN_AS" env "HOME=$(getent passwd "$RUN_AS" | cut -d: -f6)")
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

if [ -d "$DATA_DIR" ]; then chmod 0700 "$DATA_DIR"; fi

step "1/4  코드 갱신"
"${AS[@]}" git -C "$APP" pull --ff-only
echo "  $("${AS[@]}" git -C "$APP" rev-parse --short HEAD) $("${AS[@]}" git -C "$APP" log -1 --pretty=%s)"

step "2/4  의존성과 클라이언트 빌드"
"${AS[@]}" "$VENV/bin/pip" install -q -r "$APP/server/requirements.txt"
(cd "$APP/client" && umask 022 && "${AS[@]}" npm ci --silent && "${AS[@]}" npm run build)
chmod -R o+rX "$APP/client/dist"

step "3/4  서비스 정지"
systemctl stop "$SERVICE"

step "4/4  서비스 시작"
systemctl start "$SERVICE"
for _ in $(seq 1 20); do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then break; fi
    sleep 0.5
done
if ! curl -fsS "$HEALTH_URL"; then
    echo
    echo "기동 실패. 로그: journalctl -u $SERVICE -n 50 --no-pager" >&2
    exit 1
fi

echo
echo "배포 완료. 계정 확인:"
echo "  sudo -u $RUN_AS $VENV/bin/python $APP/server/manage_accounts.py list"
