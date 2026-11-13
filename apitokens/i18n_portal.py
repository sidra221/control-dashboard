"""نصوص بوابة التاجر: portal_home_ (الرئيسية)، portal_accept_ (الموافقة)، portal_login_ و portal_register_ و portal_signup_ (الدخول والتسجيل)، portal_otp_ (تحقق الواتساب)، portal_pending_ (بانتظار الربط)."""

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

    # ===== portal/login.html — دخول التاجر برقم الهاتف وكلمة المرور =====
    "portal_login_title": {"ar": "تسجيل الدخول", "en": "Sign in"},
    "portal_login_sub": {"ar": "لوحة المتجر", "en": "Store dashboard"},
    "portal_login_phone": {"ar": "رقم الهاتف", "en": "Phone number"},
    "portal_login_phone_hint": {"ar": "مع رمز الدولة، مثلاً ⁦+966501234567⁩", "en": "With country code, e.g. +966501234567"},
    "portal_login_password": {"ar": "كلمة المرور", "en": "Password"},
    "portal_login_button": {"ar": "دخول", "en": "Sign in"},
    "portal_login_forgot": {
        "ar": "نسيت كلمة المرور؟ اطلب من مزوّد المتجر رابط تسجيل جديد.",
        "en": "Forgot your password? Ask the store provider for a new registration link.",
    },
    "portal_login_create_account": {"ar": "إنشاء حساب جديد", "en": "Create an account"},
    "portal_login_err_generic": {
        "ar": "رقم الهاتف أو كلمة المرور غير صحيحة.",
        "en": "The phone number or password is incorrect.",
    },
    "portal_login_err_locked": {
        "ar": "محاولات كتير. جرّب مرة تانية بعد شوي.",
        "en": "Too many attempts. Please try again later.",
    },

    # ===== portal/register.html — إنشاء الحساب برابط الدعوة =====
    "portal_register_title": {"ar": "إنشاء حساب", "en": "Create your account"},
    "portal_register_sub": {"ar": "خطوة وحدة وبتوصل للوحة متجرك", "en": "One step to reach your store dashboard"},
    "portal_register_intro_prefix": {"ar": "رح تنشئ الحساب لمتجر «", "en": "You are creating the account for the store \u201c"},
    "portal_register_intro_suffix": {
        "ar": "». رقم الهاتف هو اللي بتسجّل فيه دخولك، ورح نبعتلك عليه كود تحقق على واتساب.",
        "en": "\u201d. Your phone number is what you use to sign in, and we will send you a verification code on WhatsApp.",
    },
    "portal_register_reset_note": {
        "ar": "هالمتجر إله حساب أصلاً. إذا كملت، الاسم والرقم وكلمة المرور القديمة بتنستبدل بالجديدة، وإذا تغيّر الرقم لازم تتحقق منه من جديد.",
        "en": "This store already has an account. If you continue, its name, phone and password are replaced by the new ones, and a changed number must be verified again.",
    },
    "portal_register_name": {"ar": "اسمك", "en": "Your name"},
    "portal_register_wa_phone": {"ar": "رقم الواتساب", "en": "WhatsApp number"},
    "portal_register_wa_phone_hint": {
        "ar": "لازم يكون رقم واتساب شغّال، مع رمز الدولة، مثلاً ⁦+966501234567⁩",
        "en": "Must be a WhatsApp number, with country code, e.g. +966501234567",
    },
    "portal_register_password": {"ar": "كلمة المرور", "en": "Password"},
    "portal_register_password_hint": {
        "ar": "8 أحرف على الأقل، وما تكون سهلة التخمين",
        "en": "At least 8 characters, and not easy to guess",
    },
    "portal_register_password_confirm": {"ar": "تأكيد كلمة المرور", "en": "Confirm password"},
    "portal_register_button": {"ar": "إنشاء الحساب", "en": "Create account"},
    "portal_register_err_name": {"ar": "اكتب اسمك.", "en": "Please enter your name."},
    "portal_register_err_name_long": {"ar": "الاسم طويل كتير.", "en": "That name is too long."},
    "portal_register_err_phone": {
        "ar": "اكتب الرقم مع رمز الدولة، مثلاً ⁦+966501234567⁩.",
        "en": "Enter the number with its country code, e.g. +966501234567.",
    },
    "portal_register_err_phone_taken": {
        "ar": "هالرقم مستخدم بحساب تاني. جرّب رقم غيره.",
        "en": "This number is already used by another account. Try a different number.",
    },
    "portal_register_err_pw_short": {
        "ar": "كلمة المرور قصيرة. لازم 8 أحرف على الأقل.",
        "en": "Password is too short. Use at least 8 characters.",
    },
    "portal_register_err_pw_long": {"ar": "كلمة المرور طويلة كتير.", "en": "Password is too long."},
    "portal_register_err_pw_common": {
        "ar": "كلمة المرور شائعة وسهلة التخمين. اختار غيرها.",
        "en": "This password is too common and easy to guess. Choose another one.",
    },
    "portal_register_err_pw_weak": {
        "ar": "كلمة المرور ضعيفة. اخلطها بأحرف وأرقام.",
        "en": "This password is too weak. Mix letters and numbers.",
    },
    "portal_register_err_mismatch": {"ar": "كلمتا المرور مو متطابقتين.", "en": "The passwords do not match."},
    "portal_auth_show_password": {"ar": "إظهار كلمة المرور", "en": "Show password"},
    "portal_auth_hide_password": {"ar": "إخفاء كلمة المرور", "en": "Hide password"},
    "portal_auth_secure_note": {"ar": "اتصال آمن ومشفّر", "en": "Secure, encrypted connection"},

    # ===== portal/signup.html — التسجيل العام (حساب معلّق بدون متجر) =====
    "portal_signup_title": {"ar": "إنشاء حساب", "en": "Create your account"},
    "portal_signup_sub": {"ar": "سجّل بياناتك وبنربط حسابك بمتجرك", "en": "Register and we will link your account to your store"},
    "portal_signup_note": {
        "ar": "بعد التسجيل بنبعتلك كود تحقق على الواتساب، وبعدها بنربط حسابك بمتجرك وبنتواصل معك.",
        "en": "After you register we send you a verification code on WhatsApp, then we link your account to your store and contact you.",
    },
    "portal_signup_have_account": {"ar": "عندك حساب؟ سجّل دخولك", "en": "Already have an account? Sign in"},
    "portal_signup_err_phone_registered": {
        "ar": "هالرقم مسجّل من قبل. جرّب تسجّل دخولك.",
        "en": "This phone number is already registered. Try signing in.",
    },
    "portal_signup_err_closed": {
        "ar": "التسجيل غير متاح حالياً. جرّب مرة تانية بعد شوي.",
        "en": "Registration is temporarily unavailable. Please try again later.",
    },

    # ===== portal/pending.html — حساب بانتظار الربط بمتجر =====
    "portal_pending_title": {"ar": "تم إنشاء حسابك", "en": "Your account was created"},
    "portal_pending_sub": {"ar": "بانتظار ربطه بمتجرك", "en": "Waiting to be linked to your store"},
    "portal_pending_hello": {"ar": "أهلاً", "en": "Welcome,"},
    "portal_pending_text": {
        "ar": "تم إنشاء حسابك. رح نربطه بمتجرك ونتواصل معك. بعد الربط بتقدر تدخل للوحة المتجر بنفس رقمك وكلمة مرورك.",
        "en": "Your account was created. We will link it to your store and contact you. Once linked, you can open the store dashboard with the same phone number and password.",
    },
    "portal_pending_phone_note": {"ar": "رقم التواصل:", "en": "Contact number:"},

    # ===== portal/verify.html — تحقق الواتساب (OTP) =====
    "portal_otp_title": {"ar": "تأكيد رقم الواتساب", "en": "Verify your WhatsApp number"},
    "portal_otp_sub": {"ar": "أدخل الكود اللي وصلك على واتساب", "en": "Enter the code we sent on WhatsApp"},
    "portal_otp_sent_to": {
        "ar": "بعتنا كود من 6 أرقام على واتساب الرقم",
        "en": "We sent a 6-digit code on WhatsApp to",
    },
    "portal_otp_code_label": {"ar": "كود التحقق", "en": "Verification code"},
    "portal_otp_code_hint": {"ar": "6 أرقام، صالح 10 دقايق", "en": "6 digits, valid for 10 minutes"},
    "portal_otp_code_title": {"ar": "6 أرقام", "en": "6 digits"},
    "portal_otp_verify_button": {"ar": "تحقّق", "en": "Verify"},
    "portal_otp_resend": {"ar": "إعادة إرسال الكود", "en": "Resend code"},
    "portal_otp_resend_in": {"ar": "إعادة الإرسال بعد {n} ث", "en": "Resend in {n} s"},
    "portal_otp_change_number": {"ar": "استخدم رقم تاني / تسجيل الخروج", "en": "Use a different number / log out"},
    "portal_otp_sent": {"ar": "بعتنالك كود جديد على واتساب.", "en": "We sent you a new code on WhatsApp."},
    "portal_otp_wait": {
        "ar": "استنى {n} ثانية قبل ما تطلب كود جديد.",
        "en": "Please wait {n} seconds before requesting a new code.",
    },
    "portal_otp_unavailable": {
        "ar": "خدمة التحقق مو متاحة مؤقتاً. جرّب مرة تانية بعد شوي.",
        "en": "Verification is temporarily unavailable. Please try again later.",
    },
    "portal_otp_throttled": {
        "ar": "طلبات كتير. جرّب مرة تانية بعد شوي.",
        "en": "Too many requests. Please try again later.",
    },
    "portal_otp_err_wrong": {
        "ar": "الكود غير صحيح. تأكد منه وجرّب مرة تانية.",
        "en": "That code is incorrect. Check it and try again.",
    },
    "portal_otp_err_expired": {
        "ar": "انتهت صلاحية الكود. اطلب كود جديد.",
        "en": "This code has expired. Request a new one.",
    },
    "portal_otp_err_exhausted": {
        "ar": "محاولات غلط كتير. اطلب كود جديد.",
        "en": "Too many incorrect attempts. Request a new code.",
    },
    "portal_otp_err_none": {
        "ar": "ما في كود فعّال. اطلب كود جديد.",
        "en": "There is no active code. Request a new one.",
    },
    "portal_otp_no_active": {
        "ar": "اضغط «إعادة إرسال الكود» لتوصلك رسالة جديدة.",
        "en": "Tap \u201cResend code\u201d to get a new message.",
    },
}
