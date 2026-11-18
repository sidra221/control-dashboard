"""نصوص بوابة التاجر: portal_home_ (الرئيسية)، portal_accept_ (الموافقة)، portal_login_ و portal_register_ و portal_signup_ (الدخول والتسجيل)، portal_otp_ (تحقق الواتساب)، portal_pending_ و portal_sub_ (صفحة الاشتراك)، portal_quota_ (رصيد تجربة اللبس وحد الزبون)."""

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
    "portal_home_range_30": {"ar": "آخر 30 يوماً", "en": "Last 30 days"},
    "portal_home_range_period": {"ar": "فترة الاشتراك الحالية", "en": "Current billing period"},
    "portal_home_filter_window": {"ar": "نافذة الربط بالطلب", "en": "Attribution window"},
    "portal_home_window_7": {"ar": "7 أيام", "en": "7 days"},
    "portal_home_window_14": {"ar": "14 يوماً", "en": "14 days"},
    "portal_home_window_30": {"ar": "30 يوماً", "en": "30 days"},
    "portal_home_apply": {"ar": "تطبيق", "en": "Apply"},

    # الحالة الفاضية
    "portal_home_empty_title": {"ar": "لا توجد بيانات بعد", "en": "No data yet"},
    "portal_home_empty_text": {
        "ar": "تبدأ البيانات بالتجمّع من أول تجربة لبس يُجريها أحد زبائنك. عُد إلى هذه الصفحة بعد قليل.",
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
            "نحتسب الطلب بقيمته الكاملة إذا جرّب الزبون أي منتج خلال النافذة المحددة قبل الشراء. "
            "يبيّن ذلك العلاقة بين التجربة والشراء، وليس دليلاً على أن التجربة هي سبب الشراء."
        ),
        "en": (
            "An order counts at its full value when the shopper tried on any product within the chosen "
            "window before buying. This shows an association between trying on and buying, not proof "
            "that the try-on caused the purchase."
        ),
    },

    # مؤشرات المحادثات
    "portal_home_conv_section": {"ar": "المحادثات والمبيعات (آخر 30 يوماً)", "en": "Conversations and sales (last 30 days)"},
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
    "portal_home_chart_empty": {"ar": "لا توجد تجارب في هذه الفترة بعد", "en": "No try-ons in this period yet"},
    "portal_home_legend_tryons": {"ar": "تجارب", "en": "Try-ons"},
    "portal_home_legend_orders": {"ar": "طلبات منسوبة", "en": "Attributed orders"},
    "portal_home_top_title": {"ar": "أكثر المنتجات تجربة", "en": "Most tried-on products"},
    "portal_home_th_product": {"ar": "المنتج", "en": "Product"},
    "portal_home_th_tryons": {"ar": "التجارب", "en": "Try-ons"},
    "portal_home_th_orders": {"ar": "الطلبات", "en": "Orders"},
    "portal_home_no_orders_badge": {"ar": "بلا طلبات", "en": "No orders"},
    "portal_home_no_orders_hint": {
        "ar": "جرّبه كثيرون ولم يشترِه أحد — قد تكون هناك مشكلة في السعر أو المقاس أو الصورة",
        "en": "Tried on a lot but nobody bought it — may signal a price, size or photo problem",
    },
    "portal_home_top_empty": {"ar": "لم تُجرَّب أي منتجات في هذه الفترة", "en": "No products were tried on in this period"},
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

    # ===== portal/login.html — دخول التاجر برقم الهاتف وكلمة المرور =====
    "portal_login_title": {"ar": "تسجيل الدخول", "en": "Sign in"},
    "portal_login_sub": {"ar": "لوحة المتجر", "en": "Store dashboard"},
    "portal_login_phone": {"ar": "رقم الهاتف", "en": "Phone number"},
    "portal_login_phone_hint": {"ar": "مع رمز الدولة، مثلاً ⁦+966501234567⁩", "en": "With country code, e.g. +966501234567"},
    "portal_login_password": {"ar": "كلمة المرور", "en": "Password"},
    "portal_login_button": {"ar": "دخول", "en": "Sign in"},
    "portal_login_forgot": {
        "ar": "يمكنك تعيين كلمة مرور جديدة برمز يصلك عبر واتساب.",
        "en": "You can set a new one with a code we send to your WhatsApp.",
    },
    "portal_login_create_account": {"ar": "إنشاء حساب جديد", "en": "Create an account"},
    "portal_login_err_generic": {
        "ar": "رقم الهاتف أو كلمة المرور غير صحيحة.",
        "en": "The phone number or password is incorrect.",
    },
    "portal_login_err_locked": {
        "ar": "محاولات كثيرة. حاول مرة أخرى بعد قليل.",
        "en": "Too many attempts. Please try again later.",
    },

    # ===== portal/register.html — إنشاء الحساب برابط الدعوة =====
    "portal_register_title": {"ar": "إنشاء حساب", "en": "Create your account"},
    "portal_register_sub": {"ar": "خطوة واحدة للوصول إلى لوحة متجرك", "en": "One step to reach your store dashboard"},
    "portal_register_intro_prefix": {"ar": "ستُنشئ الحساب لمتجر «", "en": "You are creating the account for the store \u201c"},
    "portal_register_intro_suffix": {
        "ar": "». ستسجّل دخولك برقم الهاتف هذا، وسنرسل إليه رمز تحقق على واتساب.",
        "en": "\u201d. Your phone number is what you use to sign in, and we will send you a verification code on WhatsApp.",
    },
    "portal_register_reset_note": {
        "ar": "لهذا المتجر حساب مسبقاً. إذا تابعت، فستحلّ البيانات الجديدة محلّ الاسم والرقم وكلمة المرور القديمة، وإذا تغيّر الرقم فيجب التحقق منه من جديد.",
        "en": "This store already has an account. If you continue, its name, phone and password are replaced by the new ones, and a changed number must be verified again.",
    },
    "portal_register_name": {"ar": "اسمك", "en": "Your name"},
    "portal_register_wa_phone": {"ar": "رقم الواتساب", "en": "WhatsApp number"},
    "portal_register_wa_phone_hint": {
        "ar": "يجب أن يكون رقم واتساب فعّالاً، مع رمز الدولة، مثلاً ⁦+966501234567⁩",
        "en": "Must be a WhatsApp number, with country code, e.g. +966501234567",
    },
    "portal_register_password": {"ar": "كلمة المرور", "en": "Password"},
    "portal_register_password_hint": {
        "ar": "8 أحرف على الأقل، وألّا تكون سهلة التخمين",
        "en": "At least 8 characters, and not easy to guess",
    },
    "portal_register_password_confirm": {"ar": "تأكيد كلمة المرور", "en": "Confirm password"},
    "portal_register_button": {"ar": "إنشاء الحساب", "en": "Create account"},
    "portal_register_err_name": {"ar": "اكتب اسمك.", "en": "Please enter your name."},
    "portal_register_err_name_long": {"ar": "الاسم طويل جداً.", "en": "That name is too long."},
    "portal_register_err_phone": {
        "ar": "اكتب الرقم مع رمز الدولة، مثلاً ⁦+966501234567⁩.",
        "en": "Enter the number with its country code, e.g. +966501234567.",
    },
    "portal_register_err_phone_taken": {
        "ar": "هذا الرقم مستخدم في حساب آخر. جرّب رقماً غيره.",
        "en": "This number is already used by another account. Try a different number.",
    },
    "portal_register_err_pw_short": {
        "ar": "كلمة المرور قصيرة. يجب ألّا تقل عن 8 أحرف.",
        "en": "Password is too short. Use at least 8 characters.",
    },
    "portal_register_err_pw_long": {"ar": "كلمة المرور طويلة جداً.", "en": "Password is too long."},
    "portal_register_err_pw_common": {
        "ar": "كلمة المرور شائعة وسهلة التخمين. اختر غيرها.",
        "en": "This password is too common and easy to guess. Choose another one.",
    },
    "portal_register_err_pw_weak": {
        "ar": "كلمة المرور ضعيفة. استخدم مزيجاً من الأحرف والأرقام.",
        "en": "This password is too weak. Mix letters and numbers.",
    },
    "portal_register_err_mismatch": {"ar": "كلمتا المرور غير متطابقتين.", "en": "The passwords do not match."},
    "portal_auth_show_password": {"ar": "إظهار كلمة المرور", "en": "Show password"},
    "portal_auth_hide_password": {"ar": "إخفاء كلمة المرور", "en": "Hide password"},
    "portal_auth_secure_note": {"ar": "اتصال آمن ومشفّر", "en": "Secure, encrypted connection"},

    # ===== portal/signup.html — التسجيل العام (حساب معلّق بدون متجر) =====
    "portal_signup_title": {"ar": "إنشاء حساب", "en": "Create your account"},
    "portal_signup_sub": {"ar": "سجّل بياناتك وسنربط حسابك بمتجرك", "en": "Register and we will link your account to your store"},
    "portal_signup_note": {
        "ar": "بعد التسجيل سنرسل إليك رمز تحقق على واتساب، ثم نربط حسابك بمتجرك ونتواصل معك.",
        "en": "After you register we send you a verification code on WhatsApp, then we link your account to your store and contact you.",
    },
    "portal_signup_have_account": {"ar": "لديك حساب؟ سجّل دخولك", "en": "Already have an account? Sign in"},
    "portal_signup_err_phone_registered": {
        "ar": "هذا الرقم مسجّل مسبقاً. جرّب تسجيل الدخول.",
        "en": "This phone number is already registered. Try signing in.",
    },
    "portal_signup_err_closed": {
        "ar": "التسجيل غير متاح حالياً. حاول مرة أخرى بعد قليل.",
        "en": "Registration is temporarily unavailable. Please try again later.",
    },

    # ===== portal/_install_card.html — تركيب المساعد بالمتجر (توكن + سطر السكربت) =====
    "portal_install_title": {"ar": "تركيب المساعد في متجرك", "en": "Install the assistant in your store"},
    "portal_install_step1": {"ar": "مفتاح متجرك", "en": "Your store key"},
    "portal_install_step2": {
        "ar": "أضف هذا السطر إلى صفحات متجرك",
        "en": "Add this line to your store pages",
    },
    "portal_install_step2_note": {
        "ar": "ضعه في قسم الأكواد المخصّصة (HTML) في إعدادات متجرك، فيظهر زر المساعد في صفحات المتجر.",
        "en": "Put it in the custom code (HTML) section of your store settings, and the assistant button appears on your store pages.",
    },
    "portal_install_step3": {"ar": "منتجات متجرك", "en": "Your products"},
    "portal_install_products": {
        "ar": "لدى المساعد {n} منتجاً من متجرك يقترح منها على زبائنك.",
        "en": "The assistant has {n} products from your store to suggest to shoppers.",
    },
    "portal_install_no_products": {
        "ar": "لا توجد منتجات بعد، والمساعد يقترح على زبائنك من منتجاتك فقط. تواصل معنا لربط متجرك على سلة أو زد أو Shopify أو لرفع منتجاتك.",
        "en": "There are no products yet, and the assistant only suggests your own products. Contact us to connect your Salla, Zid or Shopify store or to upload your products.",
    },
    "portal_install_token_note": {
        "ar": "هذا المفتاح خاص بمتجرك، فلا تضعه في متجر آخر.",
        "en": "This token belongs to your store; do not use it on another store.",
    },
    "portal_install_copy": {"ar": "نسخ", "en": "Copy"},
    "portal_install_copied": {"ar": "تم النسخ ✓", "en": "Copied ✓"},

    "portal_support_whatsapp": {"ar": "تواصل معنا عبر واتساب", "en": "Contact us on WhatsApp"},

    # ===== portal/pending.html — صفحة الاشتراك (حساب موثّق بلا متجر) =====
    "portal_pending_title": {"ar": "فعّل اشتراكك", "en": "Activate your subscription"},
    "portal_pending_sub": {"ar": "خطوة أخيرة لتشغيل الخدمة في متجرك", "en": "One last step to run the service in your store"},
    "portal_pending_hello": {"ar": "أهلاً", "en": "Welcome,"},
    "portal_pending_text": {
        "ar": "حسابك جاهز. اطلب كود الاشتراك وسيصلك على واتساب، ثم أدخله هنا لتفتح لوحة متجرك.",
        "en": "Your account is ready. Request your subscription code, we will send it on WhatsApp, then enter it here to open your store dashboard.",
    },
    "portal_sub_features_title": {"ar": "ماذا تقدّم الخدمة لمتجرك", "en": "What the service does for your store"},
    "portal_sub_f1_title": {"ar": "مساعد تسوق ذكي", "en": "AI shopping assistant"},
    "portal_sub_f1_text": {
        "ar": "يحاور زبائنك داخل متجرك، ويقترح عليهم المنتجات المناسبة من منتجاتك، ويساعدهم في اختيار المقاس.",
        "en": "Chats with your shoppers inside your store, suggests the right items from your catalog and helps them pick a size.",
    },
    "portal_sub_f2_title": {"ar": "تجربة اللبس الافتراضية", "en": "Virtual try-on"},
    "portal_sub_f2_text": {
        "ar": "يرفع الزبون صورته ويرى القطعة عليه قبل أن يشتري.",
        "en": "Shoppers upload a photo and see the item on themselves before they buy.",
    },
    "portal_sub_f3_title": {"ar": "ربط مع سلة وزد وShopify", "en": "Works with Salla, Zid and Shopify"},
    "portal_sub_f3_text": {
        "ar": "يمكن ربط الخدمة بمتجرك على إحدى هذه المنصات لمزامنة منتجاتك.",
        "en": "The service can be connected to your store on these platforms to sync your products.",
    },
    "portal_sub_f4_title": {"ar": "لوحة إحصائيات", "en": "Stats dashboard"},
    "portal_sub_f4_text": {
        "ar": "تتابع فيها عدد التجارب والطلبات والمبيعات التي جاءت بعد التجربة.",
        "en": "Track try-ons, orders and the sales that followed a try-on.",
    },
    "portal_sub_plan": {
        "ar": "يتضمن الاشتراك {n} تجربة لبس شهرياً.",
        "en": "The subscription includes {n} try-ons per month.",
    },
    "portal_sub_store_label": {"ar": "اسم المتجر", "en": "Store name"},
    "portal_sub_store_hint": {"ar": "كما يظهر لزبائنك", "en": "As your shoppers see it"},
    "portal_sub_request_button": {"ar": "اطلب كود الاشتراك", "en": "Request subscription code"},
    "portal_sub_sent_to": {
        "ar": "أرسلنا كود الاشتراك على واتساب إلى الرقم",
        "en": "We sent your subscription code on WhatsApp to",
    },
    "portal_sub_whatsapp_note": {
        "ar": "تصلك الرسالة من خدمة التحقق، والرمز المكوّن من 8 أحرف وأرقام فيها هو كود اشتراكك.",
        "en": "The message comes from our verification service; the 8-character code in it is your subscription code.",
    },
    "portal_sub_store_for": {"ar": "المتجر:", "en": "Store:"},
    "portal_sub_wait_reason": {
        "ar": "خدمة واتساب تسمح برسالة واحدة كل فترة لنفس الرقم، ووصلتك قبل قليل رسالة التحقق، لذلك يتاح الطلب بعد انتهاء العدّاد.",
        "en": "WhatsApp allows one message per number every so often, and your verification message arrived a moment ago, so the request opens when the countdown ends.",
    },
    "portal_sub_code_label": {"ar": "كود الاشتراك", "en": "Subscription code"},
    "portal_sub_code_hint": {"ar": "8 أحرف وأرقام، صالح 30 دقيقة", "en": "8 letters and digits, valid for 30 minutes"},
    "portal_sub_code_title": {"ar": "8 أحرف وأرقام", "en": "8 letters and digits"},
    "portal_sub_activate_button": {"ar": "فعّل الاشتراك", "en": "Activate subscription"},
    "portal_sub_err_name": {
        "ar": "أدخل اسم متجرك (حرفان على الأقل).",
        "en": "Enter your store name (at least 2 characters).",
    },
    "portal_sub_err_wrong": {
        "ar": "الكود غير صحيح. تأكد منه وحاول مرة أخرى.",
        "en": "That code is incorrect. Check it and try again.",
    },
    "portal_sub_err_expired": {
        "ar": "انتهت صلاحية الكود. اطلب كوداً جديداً.",
        "en": "This code has expired. Request a new one.",
    },
    "portal_sub_err_exhausted": {
        "ar": "تجاوزت عدد المحاولات المسموح. اطلب كوداً جديداً.",
        "en": "Too many wrong attempts. Request a new code.",
    },
    "portal_sub_change_name": {"ar": "تغيير اسم المتجر", "en": "Change store name"},
    "portal_sub_err_closed": {
        "ar": "تفعيل الاشتراكات الجديدة غير متاح حالياً. حاول مرة أخرى لاحقاً.",
        "en": "New subscriptions are temporarily unavailable. Please try again later.",
    },
    "portal_sub_err_none": {
        "ar": "لا يوجد كود فعّال. اطلب كود الاشتراك أولاً.",
        "en": "There is no active code. Request your subscription code first.",
    },

    # ===== portal/verify.html — تحقق الواتساب (OTP) =====
    "portal_otp_title": {"ar": "تأكيد رقم الواتساب", "en": "Verify your WhatsApp number"},
    "portal_otp_sub": {"ar": "أدخل الرمز الذي وصلك على واتساب", "en": "Enter the code we sent on WhatsApp"},
    "portal_otp_sent_to": {
        "ar": "أرسلنا رمزاً من 6 أرقام عبر واتساب إلى الرقم",
        "en": "We sent a 6-digit code on WhatsApp to",
    },
    "portal_otp_code_label": {"ar": "رمز التحقق", "en": "Verification code"},
    "portal_otp_code_hint": {"ar": "6 أرقام، صالح لمدة 10 دقائق", "en": "6 digits, valid for 10 minutes"},
    "portal_otp_code_title": {"ar": "6 أرقام", "en": "6 digits"},
    "portal_otp_verify_button": {"ar": "تحقّق", "en": "Verify"},
    "portal_otp_resend": {"ar": "إعادة إرسال الرمز", "en": "Resend code"},
    "portal_otp_resend_in": {"ar": "إعادة الإرسال بعد {n} ث", "en": "Resend in {n} s"},
    "portal_otp_change_number": {"ar": "استخدام رقم آخر / تسجيل الخروج", "en": "Use a different number / log out"},
    "portal_otp_sent": {"ar": "أرسلنا إليك رمزاً جديداً على واتساب.", "en": "We sent you a new code on WhatsApp."},
    "portal_otp_wait": {
        "ar": "انتظر {n} ثانية قبل طلب رمز جديد.",
        "en": "Please wait {n} seconds before requesting a new code.",
    },
    "portal_otp_unavailable": {
        "ar": "خدمة التحقق غير متاحة مؤقتاً. حاول مرة أخرى بعد قليل.",
        "en": "Verification is temporarily unavailable. Please try again later.",
    },
    "portal_otp_throttled": {
        "ar": "طلبات كثيرة. حاول مرة أخرى بعد قليل.",
        "en": "Too many requests. Please try again later.",
    },
    "portal_otp_err_wrong": {
        "ar": "الرمز غير صحيح. تأكد منه وحاول مرة أخرى.",
        "en": "That code is incorrect. Check it and try again.",
    },
    "portal_otp_err_expired": {
        "ar": "انتهت صلاحية الرمز. اطلب رمزاً جديداً.",
        "en": "This code has expired. Request a new one.",
    },
    "portal_otp_err_exhausted": {
        "ar": "محاولات خاطئة كثيرة. اطلب رمزاً جديداً.",
        "en": "Too many incorrect attempts. Request a new code.",
    },
    "portal_otp_err_none": {
        "ar": "لا يوجد رمز فعّال. اطلب رمزاً جديداً.",
        "en": "There is no active code. Request a new one.",
    },
    "portal_otp_no_active": {
        "ar": "اضغط «إعادة إرسال الرمز» لتصلك رسالة جديدة.",
        "en": "Tap \u201cResend code\u201d to get a new message.",
    },

    # ===== رصيد تجربة اللبس وحد الزبون (portal_quota_) =====
    "portal_quota_title": {"ar": "رصيد تجربة اللبس", "en": "Try-on credit"},
    "portal_quota_left_label": {"ar": "تجربة لبس متبقية من رصيدك", "en": "try-ons left in your credit"},
    "portal_quota_aria": {"ar": "نسبة الرصيد المستهلك", "en": "Share of credit used"},
    "portal_quota_used_line": {
        "ar": "استهلكت {used} من {total} ({pct}%)",
        "en": "Used {used} of {total} ({pct}%)",
    },
    "portal_quota_renews_on": {"ar": "يتجدد رصيدك بتاريخ", "en": "Your credit renews on"},
    "portal_quota_renews_next_use": {
        "ar": "انتهت فترتك الحالية، وسيتجدد رصيدك كاملاً مع أول تجربة جديدة.",
        "en": "Your last period has ended; your credit renews in full with the next try-on.",
    },
    "portal_quota_daily_line": {
        "ar": "تجارب اليوم (آخر 24 ساعة): {used} من {cap}",
        "en": "Today's try-ons (last 24 hours): {used} of {cap}",
    },
    "portal_quota_daily_note": {
        "ar": "يوجد سقف يومي للمتجر يحميك من نفاد الرصيد بسرعة (مثلاً بسبب استخدام غير معتاد).",
        "en": "Your store has a daily cap that protects your credit from running out fast (for example because of unusual traffic).",
    },
    "portal_quota_daily_reached": {
        "ar": "وصلت إلى السقف اليومي. ستُتاح التجربة مجدداً بشكل تدريجي خلال 24 ساعة.",
        "en": "You have reached today's cap. Try-on opens up again gradually over the next 24 hours.",
    },
    "portal_quota_shared_explain": {
        "ar": (
            "رصيدك حوض واحد مشترك بين كل زبائن متجرك: كل تجربة لبس ناجحة تُنقص منه وحدة، "
            "ولا نقسّمه عليهم مسبقاً. المحادثة مع المساعد مشمولة بسعر اشتراكك ولا تُحتسب من الرصيد."
        ),
        "en": (
            "Your credit is one pool shared by all of your shoppers: every successful try-on takes one from it, "
            "and it is not split between them in advance. Chatting with the assistant is included in your "
            "subscription and does not count against the credit."
        ),
    },
    "portal_quota_low_title": {"ar": "رصيدك على وشك النفاد", "en": "Your credit is running low"},
    "portal_quota_low_text": {
        "ar": (
            "استهلكت {pct}% من الرصيد وتبقّى {remaining}. عند نفاده، تتوقف تجربة اللبس لدى زبائنك "
            "حتى يتجدد. تواصل معنا إذا أردت زيادته."
        ),
        "en": (
            "You have used {pct}% of your credit and {remaining} are left. When it runs out, try-on pauses for "
            "your shoppers until it renews. Contact us if you want to increase it."
        ),
    },
    "portal_quota_exhausted_title": {"ar": "نفد رصيد تجربة اللبس", "en": "Your try-on credit is used up"},
    "portal_quota_exhausted_text": {
        "ar": (
            "تجربة اللبس متوقفة لدى زبائنك حتى يتجدد رصيدك، ويستمر المساعد الذكي (المحادثة) في العمل. "
            "تواصل معنا إذا أردت زيادة الرصيد."
        ),
        "en": (
            "Try-on is paused for your shoppers until your credit renews. The AI assistant (chat) keeps working. "
            "Contact us if you want to increase your credit."
        ),
    },
    "portal_quota_set_title": {"ar": "حد التجارب لكل زبون", "en": "Try-ons per shopper"},
    "portal_quota_set_label": {"ar": "عدد التجارب لكل زبون في اليوم", "en": "Try-ons per shopper per day"},
    "portal_quota_set_unlimited": {"ar": "بلا حد", "en": "Unlimited"},
    "portal_quota_set_explain": {
        "ar": (
            "هذا هو عدد التجارب الناجحة التي يستطيع الزبون الواحد إجراءها خلال 24 ساعة. الرقم الأقل يعني أن "
            "الرصيد يكفي عدداً أكبر من الزبائن، والرقم الأكبر يعني أن كل زبون يجرّب أكثر فينفد الرصيد أسرع. "
            "خيار «بلا حد» يسمح لزبون واحد باستهلاك الرصيد كله."
        ),
        "en": (
            "This is how many successful try-ons a single shopper can make in 24 hours. A lower number lets "
            "your credit serve more shoppers; a higher number lets each shopper try more but uses the credit "
            "faster. \u201cUnlimited\u201d lets a single shopper use up all of your credit."
        ),
    },
    "portal_quota_set_save": {"ar": "حفظ", "en": "Save"},
    "portal_quota_set_saved": {"ar": "تم حفظ الإعداد.", "en": "Setting saved."},
    "portal_quota_set_error": {
        "ar": "القيمة غير صالحة، ولم يتغيّر شيء. اختر قيمة من القائمة.",
        "en": "That value is not valid and nothing was changed. Pick one from the list.",
    },

    # ===== portal password reset =====
    "portal_reset_link": {"ar": "نسيت كلمة المرور؟", "en": "Forgot your password?"},
    "portal_reset_forgot_title": {"ar": "استعادة كلمة المرور", "en": "Reset your password"},
    "portal_reset_forgot_sub": {
        "ar": "سنرسل رمز إعادة التعيين إلى رقم واتساب المسجّل في حسابك",
        "en": "We will send a reset code to the WhatsApp number on your account",
    },
    "portal_reset_phone": {"ar": "رقم الهاتف", "en": "Phone number"},
    "portal_reset_phone_hint": {
        "ar": "الرقم الذي تسجّل الدخول به، مع رمز الدولة، مثل ⁦+966501234567⁩",
        "en": "The number you sign in with, including the country code, e.g. +966501234567",
    },
    "portal_reset_send_button": {"ar": "إرسال الرمز", "en": "Send code"},
    "portal_reset_back_login": {"ar": "العودة إلى تسجيل الدخول", "en": "Back to sign in"},
    "portal_reset_err_phone": {
        "ar": "أدخل الرقم مع رمز الدولة، مثل ⁦+966501234567⁩.",
        "en": "Enter the number with its country code, e.g. +966501234567.",
    },
    "portal_reset_err_throttled": {
        "ar": "عدد الطلبات كبير. يُرجى المحاولة مرة أخرى بعد قليل.",
        "en": "Too many requests. Please try again later.",
    },
    "portal_reset_title": {"ar": "تعيين كلمة مرور جديدة", "en": "Set a new password"},
    "portal_reset_sub": {
        "ar": "أدخل الرمز الذي وصلك عبر واتساب، ثم اختر كلمة مرور جديدة",
        "en": "Enter the code you received on WhatsApp, then choose a new password",
    },
    "portal_reset_sent_prefix": {
        "ar": "إذا كان هناك حساب موثّق مسجّل بالرقم",
        "en": "If a verified account is registered to",
    },
    "portal_reset_sent_suffix": {
        "ar": "، فقد أرسلنا إليه رمزًا من 6 أرقام عبر واتساب.",
        "en": ", we have sent a 6-digit code to it on WhatsApp.",
    },
    "portal_reset_delivery_note": {
        "ar": "قد يستغرق وصول الرسالة دقيقة، ولا يمكن طلب رمز جديد إلا بعد مهلة قصيرة.",
        "en": "The message can take a minute to arrive. A new code can only be requested after a short wait.",
    },
    "portal_reset_code_label": {"ar": "رمز إعادة التعيين", "en": "Reset code"},
    "portal_reset_code_hint": {"ar": "6 أرقام، صالح لمدة 10 دقائق", "en": "6 digits, valid for 10 minutes"},
    "portal_reset_code_title": {"ar": "6 أرقام", "en": "6 digits"},
    "portal_reset_password": {"ar": "كلمة المرور الجديدة", "en": "New password"},
    "portal_reset_password_hint": {
        "ar": "8 أحرف على الأقل، ويجب ألّا تكون سهلة التخمين",
        "en": "At least 8 characters, and not easy to guess",
    },
    "portal_reset_password_confirm": {"ar": "تأكيد كلمة المرور الجديدة", "en": "Confirm new password"},
    "portal_reset_button": {"ar": "حفظ كلمة المرور وتسجيل الدخول", "en": "Save password and sign in"},
    "portal_reset_resend": {"ar": "لم يصلك الرمز؟ اطلب رمزًا جديدًا", "en": "No code? Request a new one"},
    "portal_reset_err_wrong": {
        "ar": "الرمز غير صحيح. تحقّق منه وأعد المحاولة.",
        "en": "That code is incorrect. Check it and try again.",
    },
    "portal_reset_err_expired": {
        "ar": "الرمز غير صالح أو انتهت صلاحيته. اطلب رمزًا جديدًا.",
        "en": "This code is invalid or has expired. Request a new one.",
    },
    "portal_reset_err_exhausted": {
        "ar": "تجاوزت عدد المحاولات المسموح به. اطلب رمزًا جديدًا.",
        "en": "Too many incorrect attempts. Request a new code.",
    },
}
