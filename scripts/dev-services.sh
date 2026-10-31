#!/bin/zsh
# Local (non-Docker) analytics services bound to 127.0.0.1. Usage: scripts/dev-services.sh start|stop|status
DATA="${SEO_DATA_DIR:-$HOME/.local/share/seo-intelligence}"
mkdir -p "$DATA/clickhouse" "$DATA/minio" "$DATA/logs"
case "$1" in
  start)
    pgrep -f "clickhouse server" >/dev/null || (cd "$DATA/clickhouse" && nohup clickhouse server -- \
      --path="$DATA/clickhouse/" --listen_host=127.0.0.1 --http_port=8123 --tcp_port=9100 \
      --mysql_port=0 --postgresql_port=0 --interserver_http_port=0 \
      --logger.log="$DATA/logs/clickhouse.log" --logger.errorlog="$DATA/logs/clickhouse.err.log" >/dev/null 2>&1 &)
    pgrep -f "minio server" >/dev/null || (MINIO_ROOT_USER="${MINIO_ROOT_USER:-seo-minio}" MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-seo-minio-dev-secret}" \
      nohup minio server "$DATA/minio" --address 127.0.0.1:9000 --console-address 127.0.0.1:9001 >"$DATA/logs/minio.log" 2>&1 &)
    brew services start redis >/dev/null 2>&1; brew services start postgresql@17 >/dev/null 2>&1
    ;;
  stop) pkill -f "clickhouse server"; pkill -f "minio server" ;;
esac
sleep 2
echo "postgres: $(pg_isready -h 127.0.0.1 -q && echo up || echo down)"
echo "redis:    $(redis-cli ping 2>/dev/null || echo down)"
echo "clickhouse: $(curl -s 127.0.0.1:8123/ping || echo down)"
echo "minio:    $(curl -s -o /dev/null -w '%{http_code}' 127.0.0.1:9000/minio/health/live || echo down)"
