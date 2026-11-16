#!/usr/bin/env bash
# بيشغّل متجر WooCommerce تجريبي بمنتجات جاهزة، وبيطبع مفاتيح الـ REST.
# مرة وحدة بالأول، وبعدها آمن للتكرار.  الإيقاف: docker compose down   (-v بيمسح كل شي)
set -euo pipefail
cd "$(dirname "$0")"
PORT="${WOO_PORT:-8088}"
export WOO_PORT="$PORT"
WP="docker compose run --rm -T wpcli wp"

docker compose up -d --wait db wordpress
echo "• منستنى ووردبريس..."
for _ in $(seq 1 60); do
  curl -fsS -o /dev/null "http://localhost:$PORT/wp-login.php" 2>/dev/null && break
  sleep 2
done

# باسورد أدمن عشوائي: غالباً بتفتح المتجر عالإنترنت عبر ngrok، فما منستعمل admin/admin.
if ! $WP core is-installed 2>/dev/null; then
  ADMIN_PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  $WP core install --url="http://localhost:$PORT" --title="Control Test Store" \
    --admin_user=admin --admin_password="$ADMIN_PASS" --admin_email=admin@example.com --skip-email
  echo "$ADMIN_PASS" > .admin-password
  chmod 600 .admin-password
fi
$WP plugin is-installed woocommerce 2>/dev/null || $WP plugin install woocommerce
$WP plugin activate woocommerce
$WP rewrite structure '/%postname%/'
# wp-cli ما بيكتب .htaccess لحاله (ما بيعرف إنه Apache) — بدونه /wp-json/ بيرجّع 404.
docker compose exec -T wordpress sh -c 'cat > /var/www/html/.htaccess && chown www-data:www-data /var/www/html/.htaccess' <<'HTACCESS'
# BEGIN WordPress
<IfModule mod_rewrite.c>
RewriteEngine On
RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]
RewriteBase /
RewriteRule ^index\.php$ - [L]
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME} !-d
RewriteRule . /index.php [L]
</IfModule>
# END WordPress
HTACCESS
$WP option update woocommerce_currency SAR
$WP option update woocommerce_onboarding_profile '{"skipped":true}' --format=json >/dev/null

echo "• منزرع المنتجات والمفتاح..."
# حاوية wp-cli منفصلة — بنمرّر الملف عبر الـ volume المشترك (/var/www/html).
docker compose cp seed.php wordpress:/var/www/html/seed.php
docker compose exec -T wordpress chmod 644 /var/www/html/seed.php
$WP eval-file /var/www/html/seed.php | tee /tmp/woo-test-seed.out
docker compose exec -T wordpress rm -f /var/www/html/seed.php

cat <<MSG

جاهز ✓
  المتجر:        http://localhost:$PORT      (أدمن: admin / الباسورد بملف .admin-password → /wp-admin)
  للداشبورد لازم https، شغّل بتيرمنال تاني:   ngrok http $PORT
  وحط رابط https تبع ngrok برابط المتجر بصفحة "ربط WooCommerce"، مع المفاتيح اللي فوق.
MSG
