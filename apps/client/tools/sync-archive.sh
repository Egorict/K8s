#!/bin/sh
# Заливка архива скриншотов на том в кластере.
#
# Содержимое архива (screens/ и data.json) намеренно НЕ входит в образ клиента:
# 800 картинок весили ~158 МБ, и каждая правка клиента заставляла CI пересобирать
# и перекачивать их, а ноду — скачивать заново. Здесь локальная копия
# отправляется прямо в том, минуя сборку образа.
#
# Запускать ТАМ, ГДЕ РАБОТАЕТ kubectl (обычно на самой ноде):
#   cd ~/K8s && git pull
#   sh apps/client/tools/sync-archive.sh
#
# Флаги:
#   --status    показать, что сейчас на томе, ничего не меняя
#   --dry-run   показать, что было бы отправлено
#
# Намеренно на sh, а не на node: на ноде Node.js может не стоять, а kubectl и
# tar есть всегда.

set -eu

NAMESPACE="${ARCHIVE_NS:-app}"
SELECTOR="app=myapp"
REMOTE_DIR="/usr/share/nginx/html/as"
REMOTE_PARENT="/usr/share/nginx/html"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LOCAL_PARENT="$SCRIPT_DIR/../html"

MODE="sync"
[ "${1:-}" = "--status" ] && MODE="status"
[ "${1:-}" = "--dry-run" ] && MODE="dry"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "✖ kubectl не найден. Запускайте скрипт там, где есть доступ к кластеру." >&2
  exit 1
fi

POD=$(kubectl -n "$NAMESPACE" get pods -l "$SELECTOR" \
        --field-selector=status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

if [ -z "$POD" ]; then
  echo "✖ Не найден работающий под с меткой $SELECTOR в namespace $NAMESPACE." >&2
  echo "  Проверьте: kubectl get pods -n $NAMESPACE" >&2
  exit 1
fi

remote_status() {
  echo "Под: $POD"
  kubectl -n "$NAMESPACE" exec "$POD" -- sh -c \
    "echo -n 'На томе: '; ls -1 $REMOTE_DIR/screens 2>/dev/null | wc -l | tr -d '\n'; \
     echo -n ' картинок, '; du -sh $REMOTE_DIR 2>/dev/null | cut -f1" \
    2>/dev/null || echo "На томе: пусто или каталог ещё не создан"
}

local_status() {
  count=$(ls -1 "$LOCAL_PARENT/as/screens" 2>/dev/null | wc -l | tr -d ' ')
  size=$(du -sh "$LOCAL_PARENT/as" 2>/dev/null | cut -f1)
  echo "Локально: ${count:-0} картинок, ${size:-0}"
}

if [ "$MODE" = "status" ]; then
  remote_status
  local_status
  exit 0
fi

if [ ! -f "$LOCAL_PARENT/as/data.json" ]; then
  echo "✖ Не найден $LOCAL_PARENT/as/data.json — заливать нечего." >&2
  exit 1
fi

local_status
if [ "$MODE" = "dry" ]; then
  echo "(--dry-run: ничего не отправлено)"
  exit 0
fi

echo "Заливаю в под $POD …"

# tar одним потоком, а не kubectl cp по файлу: 800 отдельных копирований заняли
# бы минуты и создали бы 800 exec-сессий. Точка входа — родитель каталога as,
# чтобы структура на томе совпала с локальной.
tar czf - -C "$LOCAL_PARENT" as/data.json as/screens \
  | kubectl -n "$NAMESPACE" exec -i "$POD" -- tar xzf - -C "$REMOTE_PARENT"

echo "✔ Готово"
remote_status
