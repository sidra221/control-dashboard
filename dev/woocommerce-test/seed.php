<?php
// بيشتغل جوا ووردبريس (wp eval-file): منتجات تجريبية + مفتاح REST بصلاحية Read/Write.
// آمن للتكرار: ما بيعيد إنشاء المنتجات الموجودة (بالـ SKU).

function seed_image($label) {
    return [['src' => 'https://placehold.co/600x800.png?text=' . rawurlencode($label)]];
}

function seed_simple($sku, $name, $price, $sale = '', $stock = 'instock', $cat = 'Dresses') {
    if (wc_get_product_id_by_sku($sku)) return;
    $p = new WC_Product_Simple();
    $p->set_name($name);
    $p->set_sku($sku);
    $p->set_regular_price($price);
    if ($sale !== '') $p->set_sale_price($sale);
    $p->set_stock_status($stock);
    $p->set_description("<p>$name — منتج تجريبي. Soft fabric, true to size.</p>");
    $p->set_short_description("$name");
    $p->set_category_ids([seed_term($cat)]);
    $p->set_status('publish');
    $id = $p->save();
    $img = media_sideload_image(seed_image($name)[0]['src'], $id, $name, 'id');
    if (!is_wp_error($img)) { $p->set_image_id($img); $p->save(); }
}

function seed_term($name) {
    $t = term_exists($name, 'product_cat');
    if (!$t) $t = wp_insert_term($name, 'product_cat');
    return (int) (is_array($t) ? $t['term_id'] : $t);
}

function seed_variable($sku, $name, $price, $sizes, $out_of_stock_sizes = []) {
    if (wc_get_product_id_by_sku($sku)) return;
    $p = new WC_Product_Variable();
    $p->set_name($name);
    $p->set_sku($sku);
    $p->set_description("<p>$name — منتج متغيّر (مقاسات). Cotton blend.</p>");
    $p->set_category_ids([seed_term('Tops')]);
    $attr = new WC_Product_Attribute();
    $attr->set_name('Size');
    $attr->set_options($sizes);
    $attr->set_visible(true);
    $attr->set_variation(true);
    $p->set_attributes([$attr]);
    $p->set_status('publish');
    $id = $p->save();
    foreach ($sizes as $size) {
        $v = new WC_Product_Variation();
        $v->set_parent_id($id);
        $v->set_attributes(['size' => $size]);
        $v->set_regular_price($price);
        $v->set_stock_status(in_array($size, $out_of_stock_sizes, true) ? 'outofstock' : 'instock');
        $v->save();
    }
    $img = media_sideload_image(seed_image($name)[0]['src'], $id, $name, 'id');
    if (!is_wp_error($img)) { $p->set_image_id($img); }
    WC_Product_Variable::sync($id);
    $p->save();
}

require_once ABSPATH . 'wp-admin/includes/media.php';
require_once ABSPATH . 'wp-admin/includes/file.php';
require_once ABSPATH . 'wp-admin/includes/image.php';

seed_simple('DRESS-001', 'Green Midi Dress', '220.00', '180.00');
seed_simple('DRESS-002', 'Black Evening Gown', '450.00', '', 'instock');
seed_simple('BAG-001', 'Leather Tote Bag', '310.00', '', 'instock', 'Bags');
seed_simple('SOLD-001', 'Sold Out Scarf', '60.00', '', 'outofstock', 'Accessories');
seed_variable('TEE-001', 'Cotton Tee', '85.00', ['S', 'M', 'L'], ['M']);

// مفتاح REST (Read/Write) — بيتولّد مرة وحدة؛ لو موجود منعرضه "موجود" (السر ما بينسترجع).
global $wpdb;
$table = $wpdb->prefix . 'woocommerce_api_keys';
$exists = $wpdb->get_var($wpdb->prepare("SELECT key_id FROM $table WHERE description = %s", 'Control test'));
if ($exists) {
    echo "KEY_EXISTS\n";
} else {
    $ck = 'ck_' . wc_rand_hash();
    $cs = 'cs_' . wc_rand_hash();
    $wpdb->insert($table, [
        'user_id' => 1, 'description' => 'Control test', 'permissions' => 'read_write',
        'consumer_key' => wc_api_hash($ck), 'consumer_secret' => $cs, 'truncated_key' => substr($ck, -7),
    ]);
    echo "CONSUMER_KEY=$ck\nCONSUMER_SECRET=$cs\n";
}
