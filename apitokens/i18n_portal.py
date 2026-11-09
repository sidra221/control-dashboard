"""نصوص صفحة بوابة التاجر (portal/home.html). كل المفاتيح بتبدأ بـ portal_home_."""

PORTAL_TRANSLATIONS = {
    "portal_home_title": {"ar": "لوحة المتجر", "en": "Store dashboard"},
    "portal_home_sub": {
        "ar": "كيف يستفيد زبائنك من تجربة اللبس والمساعد الذكي",
        "en": "How your shoppers use virtual try-on and the AI assistant",
    },
    "portal_home_logout": {"ar": "تسجيل الخروج", "en": "Log out"},
    "portal_home_lang_label": {"ar": "اللغة", "en": "Language"},

    # الفلاتر
    "portal_home_filter_range": {"ar": "الفترة", "en": "Period"},
    "portal_home_range_7": {"ar": "آخر 7 أيام", "en": "Last 7 days"},
    "portal_home_range_30": {"ar": "آخر 30 يوم", "en": "Last 30 days"},
    "portal_home_range_period": {"ar": "فترة الاشتراك الحالية", "en": "Current billing period"},
    "portal_home_filter_window": {"ar": "نافذة الربط بالطلب", "en": "Attribution window"},
    "portal_home_window_7": {"ar": "7 أيام", "en": "7 days"},
    "portal_home_window_14": {"ar": "14 يوم", "en": "14 days"},
    "portal_home_window_30": {"ar": "30 يوم", "en": "30 days"},
    "portal_home_apply": {"ar": "تطبيق", "en": "Apply"},

    # الحالة الفاضية
    "portal_home_empty_title": {"ar": "لسا ما في بيانات", "en": "No data yet"},
    "portal_home_empty_text": {
        "ar": "البيانات بتبلّش تتجمّع من أول تجربة لبس يعملها زبون عندك. ارجع شوف الصفحة بعد شوي.",
        "en": "Data starts collecting from the first try-on a shopper makes in your store. Check back soon.",
    },

    # بطاقات تجربة اللبس
    "portal_home_tryon_section": {"ar": "تجربة اللبس", "en": "Virtual try-on"},
    "portal_home_stat_total": {"ar": "إجمالي التجارب", "en": "Total try-ons"},
    "portal_home_stat_unique": {"ar": "زبون جرّب", "en": "Unique shoppers"},
    "portal_home_stat_per_shopper": {"ar": "تجربة لكل زبون", "en": "Try-ons per shopper"},
    "portal_home_stat_revenue": {"ar": "مبيعات منسوبة للتجربة", "en": "Attributed revenue"},
    "portal_home_orders_line": {"ar": "طلب منسوب للتجربة", "en": "attributed orders"},
    "portal_home_revenue_explain": {
        "ar": (
            "بنحسب الطلب بقيمته الكاملة إذا الزبون جرّب أي منتج خلال النافذة المحددة قبل ما يشتري. "
            "هاد بيبيّن العلاقة بين التجربة والشراء، مش دليل إنه التجربة هي سبب الشراء."
        ),
        "en": (
            "An order counts at its full value when the shopper tried on any product within the chosen "
            "window before buying. This shows an association between trying on and buying, not proof "
            "that the try-on caused the purchase."
        ),
    },

    # مؤشرات المحادثات
    "portal_home_conv_section": {"ar": "المحادثات والمبيعات (آخر 30 يوم)", "en": "Conversations and sales (last 30 days)"},
    "portal_home_conv_total": {"ar": "محادثات", "en": "Conversations"},
    "portal_home_conv_recommendation": {"ar": "محادثات فيها ترشيح منتجات", "en": "With a product recommendation"},
    "portal_home_conv_intent": {"ar": "نية شراء", "en": "Purchase intent"},
    "portal_home_conv_orders": {"ar": "طلبات", "en": "Orders"},
    "portal_home_conv_revenue": {"ar": "المبيعات", "en": "Revenue"},

    # الشارت والجدول
    "portal_home_chart_title": {"ar": "التجارب والطلبات يومياً", "en": "Daily try-ons and orders"},
    "portal_home_chart_aria": {
        "ar": "مخطط يومي: أعمدة التجارب وخط الطلبات المنسوبة",
        "en": "Daily chart: try-on bars and an attributed-orders line",
    },
    "portal_home_chart_empty": {"ar": "ما في تجارب بهالفترة لسا", "en": "No try-ons in this period yet"},
    "portal_home_legend_tryons": {"ar": "تجارب", "en": "Try-ons"},
    "portal_home_legend_orders": {"ar": "طلبات منسوبة", "en": "Attributed orders"},
    "portal_home_top_title": {"ar": "أكثر المنتجات تجربة", "en": "Most tried-on products"},
    "portal_home_th_product": {"ar": "المنتج", "en": "Product"},
    "portal_home_th_tryons": {"ar": "التجارب", "en": "Try-ons"},
    "portal_home_th_orders": {"ar": "الطلبات", "en": "Orders"},
    "portal_home_no_orders_badge": {"ar": "بلا طلبات", "en": "No orders"},
    "portal_home_no_orders_hint": {
        "ar": "جرّبوه كتير وما اشتراه حدا — ممكن يكون في مشكلة بالسعر أو المقاس أو الصورة",
        "en": "Tried on a lot but nobody bought it — may signal a price, size or photo problem",
    },
    "portal_home_top_empty": {"ar": "ما في منتجات انجرّبت بهالفترة", "en": "No products were tried on in this period"},
    # ===== portal/accept.html — موافقة التاجر على سياسة الخصوصية =====
    "portal_accept_title": {"ar": "الموافقة على سياسة الخصوصية", "en": "Privacy policy agreement"},
    "portal_accept_sub": {"ar": "خطوة واحدة قبل عرض لوحة المتجر", "en": "One step before your dashboard"},
    "portal_accept_intro": {
        "ar": "قبل استخدام لوحة المتجر وخدمة المساعد الذكي وتجربة اللبس، يرجى الاطلاع على سياسة الخصوصية والموافقة عليها.",
        "en": "Before using the store dashboard, the AI assistant and the virtual try-on, please read and agree to the privacy policy.",
    },
    "portal_accept_point_1": {
        "ar": "تُعالَج بيانات متجرك (المنتجات والطلبات والمحادثات) لأغراض تشغيل الخدمة فقط.",
        "en": "Your store data (products, orders and conversations) is processed only to run the service.",
    },
    "portal_accept_point_2": {
        "ar": "عند استخدام تجربة اللبس، تُعالَج الصور التي يرفعها زبائنك لإنتاج صورة التجربة، وفق القسم الخاص بذلك في السياسة.",
        "en": "When the try-on is used, photos uploaded by your shoppers are processed to produce the try-on image, as described in the dedicated section of the policy.",
    },
    "portal_accept_point_3": {
        "ar": "نستعين بمزوّدين خارجيين لمعالجة النصوص والصور، وقد تُعالَج البيانات خارج بلدك.",
        "en": "We use external providers to process text and images, and data may be processed outside your country.",
    },
    "portal_accept_point_4": {
        "ar": "لا نبيع البيانات ولا نستخدمها للإعلانات.",
        "en": "We do not sell data or use it for advertising.",
    },
    "portal_accept_read": {"ar": "قراءة سياسة الخصوصية كاملة", "en": "Read the full privacy policy"},
    "portal_accept_checkbox": {
        "ar": "أقرّ بأنني اطّلعت على سياسة الخصوصية وأوافق على معالجة البيانات الموضحة فيها لأغراض تشغيل الخدمة في متجري.",
        "en": "I confirm that I have read the privacy policy and agree to the processing of data described in it to run the service for my store.",
    },
    "portal_accept_button": {"ar": "أوافق وأتابع", "en": "I agree and continue"},
    "portal_accept_error": {"ar": "يجب تعليم خانة الموافقة للمتابعة.", "en": "Please tick the agreement box to continue."},
    "portal_accept_version": {"ar": "نسخة السياسة", "en": "Policy version"},
}
