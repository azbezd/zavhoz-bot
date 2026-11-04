#!/bin/sh
# Build before touching checkout. No mtime test: SQLite WAL can change independently.
set -eu
umask 077
exec 9>/opt/zavhoz/.publish-site.lock
flock -n 9 || exit 0
cd /opt/zavhoz/zavhoz-bot
export INVENTORY_DB=/opt/zavhoz/inventory.db
export INVENTORY_REPO_DIR=/opt/zavhoz/zavhoz-inventory
WEB=/opt/zavhoz/zavhoz-web-git
STAGE=$(mktemp -d /opt/zavhoz/.site-stage.XXXXXX)
trap 'rm -rf "$STAGE"' EXIT HUP INT TERM
python3 -m inventory_bot.build_site "$STAGE"
# Branch must be selected explicitly; never overwrite a developer checkout.
BRANCH=$(git -C "$WEB" branch --show-current)
[ "$BRANCH" = "${CATALOG_PUBLISH_BRANCH:-main}" ] || exit 1
for name in assets photos item index.html 404.html .nojekyll; do
  rsync -a --delete "$STAGE/$name" "$WEB/"
done
git -C "$WEB" add -- assets photos item index.html 404.html .nojekyll
if ! git -C "$WEB" diff --cached --quiet; then
  git -C "$WEB" commit -m 'Обновлён публичный справочник компонентов'
fi
# Retry an earlier failed push even if content has not changed.
git -C "$WEB" push origin "$BRANCH"
