"""ترجمات واجهة الداشبورد (عربي/إنجليزي). قاموس بسيط بدون gettext —
كافي لعدد محدود من الصفحات الداخلية، وما بيحتاج أدوات بناء إضافية.
"""

LANGUAGES = ("ar", "en")
DEFAULT_LANGUAGE = "ar"

TRANSLATIONS = {
    # ===== base.html =====
    "admin_back": {"ar": "رجوع للوحة الأدمن ←", "en": "Back to admin ←"},
    "copied": {"ar": "تم النسخ ✓", "en": "Copied ✓"},

    # ===== dashboard.html =====
    "dashboard_title": {"ar": "توكنات kie.ai", "en": "kie.ai Tokens"},
    "dashboard_sub": {"ar": "إدارة توكنات العملاء وحدود الاستهلاك", "en": "Manage client tokens and usage limits"},
    "stat_total": {"ar": "إجمالي التوكنات", "en": "Total tokens"},
    "stat_active": {"ar": "فعّالة حالياً", "en": "Currently active"},
    "stat_images_used": {"ar": "صورة مُستهلكة", "en": "Images used"},
    "new_token_prefix": {"ar": "التوكن تبع \"", "en": "Token for \""},
    "new_token_suffix": {"ar": "\" — انسخه وابعتو للمستخدم:", "en": "\" — copy it and send it to the user:"},
    "copy": {"ar": "نسخ", "en": "Copy"},
    "generate_new_token": {"ar": "توليد توكن جديد", "en": "Generate new token"},
    "label_client_name": {"ar": "اسم العميل / المتجر", "en": "Client / store name"},
    "placeholder_client_name": {"ar": "مثلاً: سوبرماركت الأمل", "en": "e.g. Al-Amal Supermarket"},
    "label_image_quota": {"ar": "عدد الصور المسموح", "en": "Allowed image quota"},
    "label_period": {"ar": "كل فترة", "en": "Every period"},
    "generate_token_btn": {"ar": "توليد توكن", "en": "Generate token"},
    "current_tokens": {"ar": "التوكنات الحالية", "en": "Current tokens"},
    "th_name": {"ar": "الاسم", "en": "Name"},
    "th_token": {"ar": "التوكن", "en": "Token"},
    "th_quota": {"ar": "عدد الصور المسموح", "en": "Image quota"},
    "th_period": {"ar": "الفترة", "en": "Period"},
    "th_used": {"ar": "المستخدم حالياً", "en": "Used now"},
    "th_status": {"ar": "الحالة", "en": "Status"},
    "th_created": {"ar": "تاريخ الإنشاء", "en": "Created at"},
    "th_catalog": {"ar": "الكتالوج", "en": "Catalog"},
    "th_discounts": {"ar": "أكواد الخصم", "en": "Discount codes"},
    "status_active": {"ar": "فعّال", "en": "Active"},
    "status_inactive": {"ar": "موقوف", "en": "Stopped"},
    "shopify_connected": {"ar": "Shopify ✓", "en": "Shopify ✓"},
    "shopify_connect_link": {"ar": "ربط Shopify", "en": "Connect Shopify"},
    "btn_stop": {"ar": "إيقاف", "en": "Stop"},
    "btn_activate": {"ar": "تفعيل", "en": "Activate"},
    "empty_tokens": {"ar": "ما في توكنات لسا — ولّد أول توكن من الفورم فوق", "en": "No tokens yet — generate the first one from the form above"},

    # ===== catalog.html =====
    "catalog_title_prefix": {"ar": "كتالوج ", "en": "Catalog "},
    "catalog_sub": {"ar": "إدارة منتجات هالتاجر لمحرّك الترشيح", "en": "Manage this merchant's products for the recommendation engine"},
    "shopify_link_short": {"ar": "ربط Shopify ←", "en": "Connect Shopify ←"},
    "back_to_tokens": {"ar": "رجوع للتوكنات ←", "en": "Back to tokens ←"},
    "upload_new_catalog": {"ar": "رفع كتالوج جديد", "en": "Upload a new catalog"},
    "upload_note": {
        "ar": "رفع ملف جديد بيعوّض كل المنتجات الحالية لهالتاجر. الأعمدة المطلوبة: sku, title, description, price, image_url, product_url, category, tags",
        "en": "Uploading a new file replaces all of this merchant's current products. Required columns: sku, title, description, price, image_url, product_url, category, tags",
    },
    "csv_file": {"ar": "ملف CSV", "en": "CSV file"},
    "upload_replace_btn": {"ar": "رفع واستبدال الكتالوج", "en": "Upload and replace catalog"},
    "current_products_prefix": {"ar": "المنتجات الحالية", "en": "Current products"},
    "th_product": {"ar": "المنتج", "en": "Product"},
    "th_category": {"ar": "الفئة", "en": "Category"},
    "th_price": {"ar": "السعر", "en": "Price"},
    "th_tags": {"ar": "الوسوم", "en": "Tags"},
    "empty_products": {"ar": "ما في منتجات لسا — ارفع ملف الكتالوج فوق", "en": "No products yet — upload the catalog file above"},

    # ===== shopify_connect.html =====
    "shopify_page_title_prefix": {"ar": "ربط Shopify — ", "en": "Connect Shopify — "},
    "shopify_page_sub": {"ar": "كتالوج حي يتحدّث لحاله من متجر Shopify الحقيقي", "en": "A live catalog that updates itself from the real Shopify store"},
    "manual_catalog_link": {"ar": "الكتالوج اليدوي ←", "en": "Manual catalog ←"},
    "current_status": {"ar": "الحالة الحالية", "en": "Current status"},
    "status_connected": {"ar": "مربوط", "en": "Connected"},
    "last_sync_prefix": {"ar": "آخر مزامنة: ", "en": "Last sync: "},
    "never_synced": {"ar": "ما صارت مزامنة لسا", "en": "Not synced yet"},
    "synced_products_count_prefix": {"ar": " — عدد المنتجات المتزامنة حالياً: ", "en": " — currently synced products: "},
    "sync_now": {"ar": "مزامنة الآن", "en": "Sync now"},
    "disconnect": {"ar": "فصل الربط", "en": "Disconnect"},
    "disconnect_confirm": {
        "ar": "متأكد؟ هاد بيفصل الربط الحي (المنتجات الحالية بتضل متل ما هي لحد ما ترفع كتالوج يدوي جديد أو تعيد الربط).",
        "en": "Are you sure? This disconnects the live sync (current products stay as they are until you upload a new manual catalog or reconnect).",
    },
    "update_connection": {"ar": "تحديث بيانات الربط", "en": "Update connection details"},
    "connect_shopify_store": {"ar": "ربط متجر Shopify", "en": "Connect a Shopify store"},
    "shopify_setup_note": {
        "ar": (
            'قبل ما تعبي الفورم، لازم تنشئ "تطبيق خاص" (Custom App) من إعدادات متجر الـ Shopify:<br>'
            "1) من لوحة تحكم المتجر: Settings ← Apps and sales channels ← Develop apps ← Create an app.<br>"
            "2) من تبويب Configuration، فعّل صلاحية <code>read_products</code> على الأقل من Admin API scopes واحفظ.<br>"
            "3) دوس Install app، وبعدها من تبويب API credentials انسخ الـ <strong>Admin API access token</strong> (بيبين مرة وحدة بس).<br>"
            "4) من نفس الصفحة انسخ الـ <strong>API secret key</strong> (موجود جنب الـ API key تبع التطبيق)."
        ),
        "en": (
            'Before filling the form, create a "Custom App" from the Shopify store settings:<br>'
            "1) From the store admin: Settings → Apps and sales channels → Develop apps → Create an app.<br>"
            "2) In the Configuration tab, enable at least the <code>read_products</code> scope under Admin API scopes and save.<br>"
            "3) Click Install app, then copy the <strong>Admin API access token</strong> from the API credentials tab (shown only once).<br>"
            "4) From the same page, copy the <strong>API secret key</strong> (next to the app's API key)."
        ),
    },
    "shop_domain_label": {"ar": "نطاق المتجر", "en": "Shop domain"},
    "access_token_label": {"ar": "Admin API Access Token", "en": "Admin API Access Token"},
    "api_secret_label": {"ar": "API Secret", "en": "API Secret"},
    "update_resync_btn": {"ar": "تحديث وإعادة مزامنة", "en": "Update and resync"},
    "connect_sync_btn": {"ar": "ربط ومزامنة", "en": "Connect and sync"},

    # ===== discount_codes.html =====
    "discounts_link_short": {"ar": "أكواد الخصم ←", "en": "Discount codes ←"},
    "discounts_title_prefix": {"ar": "أكواد خصم — ", "en": "Discount codes — "},
    "discounts_sub": {"ar": "أكواد يقدر المساعد الذكي يعرضها على زبون قرّر يشتري", "en": "Codes the AI assistant can offer a customer who's decided to buy"},
    "add_discount_code": {"ar": "إضافة كود خصم", "en": "Add a discount code"},
    "discount_code_label": {"ar": "الكود", "en": "Code"},
    "discount_description_label": {"ar": "الوصف", "en": "Description"},
    "discount_description_placeholder": {"ar": "مثلاً: خصم 10% على كل المنتجات", "en": "e.g. 10% off everything"},
    "add_discount_btn": {"ar": "إضافة", "en": "Add"},
    "current_discount_codes": {"ar": "الأكواد الحالية", "en": "Current codes"},
    "th_code": {"ar": "الكود", "en": "Code"},
    "th_description": {"ar": "الوصف", "en": "Description"},
    "empty_discount_codes": {"ar": "ما في أكواد خصم لسا — ضيف أول كود من الفورم فوق", "en": "No discount codes yet — add the first one from the form above"},

    # ===== view messages (views.py) =====
    "err_label_required": {"ar": "لازم تكتب اسم للتعريف", "en": "You must enter a name"},
    "err_quota_invalid": {"ar": "عدد الصور لازم يكون رقم أكبر من صفر", "en": "Image quota must be a number greater than zero"},
    "err_period_invalid": {"ar": "فترة غير صحيحة", "en": "Invalid period"},
    "msg_token_created": {"ar": "تم إنشاء التوكن — انسخه وابعتو للمستخدم", "en": "Token created — copy it and send it to the user"},
    "err_csv_required": {"ar": "لازم تختار ملف CSV", "en": "You must choose a CSV file"},
    "msg_catalog_replaced": {"ar": "تم استبدال الكتالوج بنجاح", "en": "Catalog replaced successfully"},
    "err_file_utf8": {"ar": "الملف لازم يكون UTF-8", "en": "The file must be UTF-8"},
    "err_missing_columns": {"ar": "أعمدة ناقصة بالملف: %(missing)s", "en": "Missing columns in file: %(missing)s"},
    "err_row_missing_field": {"ar": "سطر %(row)s: sku أو title أو image_url ناقص", "en": "Row %(row)s: sku, title, or image_url is missing"},
    "err_row_bad_price": {"ar": "سطر %(row)s: السعر '%(price)s' مش رقم صحيح", "en": "Row %(row)s: price '%(price)s' is not a valid number"},
    "err_empty_csv": {"ar": "الملف ما فيه ولا صف منتج", "en": "The file has no product rows"},
    "msg_shopify_disconnected": {"ar": "تم فصل الربط مع Shopify", "en": "Disconnected from Shopify"},
    "err_no_connection_to_resync": {"ar": "ما في ربط حالياً مشان نعيد المزامنة", "en": "No connection exists to resync"},
    "msg_resync_success": {"ar": "تمت إعادة المزامنة بنجاح", "en": "Resynced successfully"},
    "err_missing_connect_fields": {"ar": "لازم تعبي نطاق المتجر، الـ Access Token، والـ API Secret", "en": "You must fill in the shop domain, Access Token, and API Secret"},
    "msg_connect_sync_success": {"ar": "تم الربط والمزامنة الأولى بنجاح — الكتالوج هلأ حي", "en": "Connected and synced successfully — the catalog is now live"},
    "err_connect_sync_partial": {"ar": "تم حفظ الربط بس صار خطأ أثناء التسجيل/المزامنة: %(exc)s", "en": "Connection saved, but an error occurred during registration/sync: %(exc)s"},
    "err_discount_code_required": {"ar": "لازم تكتب الكود", "en": "You must enter the code"},
    "msg_discount_code_added": {"ar": "تم إضافة كود الخصم", "en": "Discount code added"},

    # ===== period choices (ApiToken.Period) =====
    "period_daily": {"ar": "يومياً", "en": "Daily"},
    "period_weekly": {"ar": "أسبوعياً", "en": "Weekly"},
    "period_monthly": {"ar": "شهرياً", "en": "Monthly"},
}


def get_translations(lang):
    if lang not in LANGUAGES:
        lang = DEFAULT_LANGUAGE
    return {key: values.get(lang, values[DEFAULT_LANGUAGE]) for key, values in TRANSLATIONS.items()}


def t(key, lang):
    if lang not in LANGUAGES:
        lang = DEFAULT_LANGUAGE
    entry = TRANSLATIONS.get(key, {})
    return entry.get(lang, entry.get(DEFAULT_LANGUAGE, key))
