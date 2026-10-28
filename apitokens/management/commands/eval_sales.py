"""تقييم آلي لأداء المساعد بالبيع على سيناريوهات زبائن ثابتة، ضد الموديل
الحقيقي المضبوط بالإعدادات. شغّله بعد أي تعديل على البرومبت أو الموديل:

    python manage.py eval_sales
    python manage.py eval_sales --model gpt-6-luna --effort medium --only gulf

كل سيناريو بيتفحص بطريقتين: فحوصات حتمية بالكود (لغة، كرت منتج، خصم، تكرار،
تنسيق) وحكم موديل (اللهجة، الإقناع، الصدق). ما بيلمس أي بيانات حقيقية — كلشي
جوا transaction بترجع لورا بالآخر.
"""

import json
import re
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.test import Client
from django.test.utils import setup_databases, setup_test_environment, teardown_databases

from apitokens.models import ApiToken, DiscountCode, Product

ARABIC = re.compile(r"[؀-ۿ]")
MARKDOWN = re.compile(r"\*\*|^#|^\s*[-*•]\s|^\s*\d+[.)]\s", re.MULTILINE)
STALL = re.compile(
    r"خليني (أ|ا)?(عرض|فرج|ورج)|لحظة|ثواني|let me show|one moment|just a moment|un instant|je vous montre",
    re.IGNORECASE,
)

CATALOG = [
    dict(sku="DRESS-SAGE", title="Sage Green Midi Dress", price="89.00", category="dresses",
         tags="green,midi,wedding guest", description="Flowing chiffon midi dress with a wrap waist and flutter sleeves. Fully lined.",
         options={"Size": ["S", "M", "L"]}),
    dict(sku="DRESS-BLACK", title="Black Satin Evening Gown", price="129.00", category="dresses",
         tags="black,evening,satin,formal", description="Floor-length satin gown with a cowl neckline and open back.",
         options={"Size": ["XS", "S", "M"]}),
    dict(sku="DRESS-RED", title="Red Floral Summer Dress", price="49.00", compare_at_price="69.00",
         category="dresses", tags="red,floral,summer,casual", description="Light cotton dress with a red floral print and adjustable straps.",
         options={"Size": ["S", "M", "L", "XL"]}),
    dict(sku="DRESS-NAVY", title="Navy Cocktail Dress", price="95.00", category="dresses",
         tags="navy,blue,cocktail,party", description="Knee-length crepe dress with a fitted bodice.",
         options={"Size": ["M"]}, in_stock=False),
    dict(sku="SHIRT-WHITE", title="White Linen Shirt", price="45.00", category="shirts",
         tags="white,linen,men,summer", description="Breathable linen shirt, relaxed fit, mother-of-pearl buttons.",
         options={"Size": ["M", "L", "XL"]}),
    dict(sku="SHIRT-OXFORD", title="Blue Oxford Shirt", price="55.00", category="shirts",
         tags="blue,men,office,formal", description="Classic cotton oxford shirt with a button-down collar.",
         options={"Size": ["S", "M", "L"]}),
    dict(sku="WAX-SKI", title="All-Temperature Ski Wax", price="15.00", category="sport",
         tags="ski,snowboard,winter", description="Fast-glide wax for skis and snowboards, works from -20 to 0 C."),
    dict(sku="BAG-GOLD", title="Gold Evening Clutch", price="35.00", category="accessories",
         tags="gold,clutch,evening", description="Metallic clutch with a detachable chain strap."),
]

STORE_INFO = (
    "Shipping: free delivery on orders over 100 USD, otherwise 7 USD. Delivery takes 2-4 business days.\n"
    "Returns: free returns within 14 days if the item is unworn with tags."
)

# turns: رسائل الزبون بالترتيب. checks: فحوصات على رد آخر دور (إلا إذا محدد غير هيك).
# judge: معايير الحكم النوعي على المحادثة كاملة.
SCENARIOS = [
    dict(name="levantine", turns=["مرحبا، بدي فستان أخضر لعرس الأسبوع الجاي"], lang="ar",
         checks={"card": True}, judge=["Reply is in Levantine Arabic (not MSA, not another dialect)",
                                       "Reply sells the benefit for a wedding, not just specs"]),
    dict(name="gulf", turns=["هلا، أبي فستان سهرة أسود، وش عندكم؟"], lang="ar",
         checks={"card": True}, judge=["Reply is in Gulf Arabic (e.g. words like أبي/وش/زين style), not Levantine or MSA"]),
    dict(name="egyptian", turns=["عايزة فستان لفرح صاحبتي، عندكم إيه؟"], lang="ar",
         checks={"card": True, "not_gift": True},
         judge=["Reply is in Egyptian Arabic",
                "Reply treats the dress as for the customer herself (she's attending), not as a gift"]),
    dict(name="english", turns=["hi, I need something for a summer brunch"], lang="en",
         checks={"card": True}, judge=["Reply is warm, persuasive and in natural English"]),
    dict(name="french", turns=["Bonjour, je cherche une robe verte pour un mariage"], lang="other",
         checks={"card": True}, judge=["Reply is entirely in French"]),
    dict(name="arabizi", turns=["marhaba, baddi fostan a7mar lal sayf"], lang="ar",
         checks={"card": True, "single_script": True},
         judge=["Reply is in Levantine Arabic written in Arabic script, with no Latin words mixed in"]),
    dict(name="neutral_gender", turns=["مرحبا بدي قميص للشغل"], lang="ar",
         checks={"card": True}, judge=["Reply does not address the customer with feminine forms (no gender was revealed)"]),
    dict(name="price_objection", turns=["بدي فستان سهرة أسود", "حلو بس غالي شوي"], lang="ar",
         checks={"discount": True, "no_repeat": True},
         judge=["On the price objection the reply defends the product's value and uses the discount, without giving up"]),
    dict(name="purchase_resurface", turns=["I want a white linen shirt", "great, how do I buy it?"], lang="en",
         checks={"card": True, "card_sku": "SHIRT-WHITE"}, judge=["Reply points the customer to the buy button/link"]),
    dict(name="real_sale", turns=["عندكم شي فستان صيفي على تخفيض؟"], lang="ar",
         checks={"card": True, "card_sku": "DRESS-RED"},
         judge=["Reply mentions the real reduced price / saving (was 69, now 49) without inventing other discounts"]),
    dict(name="out_of_stock", turns=["I'd like a navy cocktail dress"], lang="en",
         checks={"card": True, "never_sku": "DRESS-NAVY"},
         judge=["Reply doesn't claim the navy dress is available; it confidently offers the closest alternative"]),
    dict(name="sizes", turns=["بدي الفستان الأخضر", "في منه مقاس XL؟"], lang="ar",
         checks={}, judge=["Reply says honestly that XL isn't available and mentions the sizes that are (S, M, L)"]),
    dict(name="shipping", turns=["how long does delivery take and can I return it?"], lang="en",
         checks={}, judge=["Reply states 2-4 business days delivery and 14-day free returns, inventing nothing else"]),
    dict(name="gift", turns=["بدي هدية لأختي، بتحب الفساتين الحمرا"], lang="ar",
         checks={"card": True, "gift": True},
         judge=["Reply and try-on invitation refer to the sister, not to the customer"]),
    dict(name="non_wearable", turns=["I need wax for my snowboard"], lang="en",
         checks={"card": True, "card_sku": "WAX-SKI", "no_tryon": True},
         judge=["Reply does not invite the customer to try the wax on with a photo"]),
    dict(name="no_fake_discount", turns=["do you have any discount codes?"], lang="en",
         checks={"discount": False}, judge=["Reply doesn't reveal or invent a discount code before a product was discussed (mentioning real sale items from the catalog is fine)"]),
]

JUDGE_PROMPT = """You are a strict QA reviewer of an e-commerce sales chatbot. Context: every \
product card the bot shows has a real photo try-on button (the customer uploads a photo and sees \
themselves wearing it) and a real buy button, so mentioning those is correct. Read the conversation \
and decide for EACH criterion whether the LAST assistant reply passes. Be strict. Reply with JSON only: \
{"results": [{"criterion": "...", "pass": true|false, "note": "<10 words"}], \
"persuasion": <1-5 how persuasive and salesmanlike the assistant was>}"""


class Command(BaseCommand):
    help = "Run the sales scenarios against the live model and score the assistant"

    def add_arguments(self, parser):
        parser.add_argument("--model", help="Override OPENAI_MODEL for this run")
        parser.add_argument("--effort", help="Override OPENAI_REASONING_EFFORT for this run")
        parser.add_argument("--only", help="Comma-separated scenario names")
        parser.add_argument("--no-judge", action="store_true", help="Skip the model-judged criteria")

    def handle(self, *args, **opts):
        if opts["model"]:
            settings.OPENAI_MODEL = opts["model"]
        if opts["effort"] is not None:
            settings.OPENAI_REASONING_EFFORT = opts["effort"]
        setup_test_environment()  # بيسمح لـ Client يحكي مع الـ views بأي ALLOWED_HOSTS
        only = set(filter(None, (opts["only"] or "").split(",")))
        scenarios = [s for s in SCENARIOS if not only or s["name"] in only]
        self.stdout.write(
            f"provider={settings.LLM_PROVIDER} model={settings.OPENAI_MODEL} "
            f"effort={settings.OPENAI_REASONING_EFFORT or '-'} scenarios={len(scenarios)}\n"
        )

        totals = {"checks": 0, "checks_ok": 0, "judge": 0, "judge_ok": 0, "persuasion": [], "secs": []}
        # SQLite: قاعدة بيانات مؤقتة بالذاكرة — transaction طويلة كانت بتقفل الملف
        # وبتوقّف السيرفر الشغّال. Postgres: transaction بترجع لورا كافية وآمنة.
        old_config = (
            setup_databases(verbosity=0, interactive=False)
            if connection.vendor == "sqlite" else None
        )
        try:
            with transaction.atomic():
                merchant = self._setup()
                for scenario in scenarios:
                    self._run(merchant, scenario, totals, judge=not opts["no_judge"])
                transaction.set_rollback(True)
        finally:
            if old_config is not None:
                teardown_databases(old_config, verbosity=0)

        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(f"code checks : {totals['checks_ok']}/{totals['checks']}")
        if totals["judge"]:
            self.stdout.write(f"judge       : {totals['judge_ok']}/{totals['judge']}")
        if totals["persuasion"]:
            avg = sum(totals["persuasion"]) / len(totals["persuasion"])
            self.stdout.write(f"persuasion  : {avg:.1f}/5")
        if totals["secs"]:
            self.stdout.write(f"avg reply   : {sum(totals['secs']) / len(totals['secs']):.1f}s")

    def _setup(self):
        merchant = ApiToken.objects.create(
            label="eval", image_quota=1, currency="USD", store_info=STORE_INFO,
        )
        for row in CATALOG:
            Product.objects.create(
                merchant=merchant, image_url="https://example.com/x.jpg", **row,
            )
        DiscountCode.objects.create(merchant=merchant, code="WELCOME10", description="10% off your first order")
        return merchant

    def _run(self, merchant, scenario, totals, judge):
        client = Client()
        history, shown, all_cards, last = [], [], [], None
        for turn in scenario["turns"]:
            history.append({"role": "user", "content": turn})
            started = time.time()
            resp = client.post(
                "/widget/chat/",
                data=json.dumps({"messages": history, "shown_skus": shown}),
                content_type="application/json",
                HTTP_X_API_TOKEN=merchant.token,
            ).json()
            totals["secs"].append(time.time() - started)
            if not resp.get("ok"):
                self.stdout.write(self.style.ERROR(f"[{scenario['name']}] request failed: {resp}"))
                return
            cards = resp.get("recommendations") or []
            all_cards.extend(cards)
            shown.extend(c["sku"] for c in cards if c["sku"] not in shown)
            history.append({"role": "assistant", "content": resp["reply"]})
            last = (resp, cards, list(shown))

        resp, cards, _ = last
        reply = resp["reply"]
        results = self._code_checks(scenario, reply, cards, all_cards, resp)
        for name, ok in results:
            totals["checks"] += 1
            totals["checks_ok"] += ok

        judged, persuasion = [], None
        if judge and scenario.get("judge"):
            judged, persuasion = self._judge(history, scenario["judge"])
            for item in judged:
                totals["judge"] += 1
                totals["judge_ok"] += bool(item.get("pass"))
            if persuasion:
                totals["persuasion"].append(persuasion)

        failed = [n for n, ok in results if not ok] + [j["criterion"] for j in judged if not j.get("pass")]
        status = self.style.SUCCESS("PASS") if not failed else self.style.ERROR("FAIL")
        self.stdout.write(f"\n[{status}] {scenario['name']}  (persuasion {persuasion or '-'}/5)")
        for turn in history:
            prefix = "  👤 " if turn["role"] == "user" else "  🤖 "
            self.stdout.write(prefix + turn["content"].replace("\n", " "))
        for card in cards:
            self.stdout.write(f"     🃏 {card['sku']} | {card['tryon_button_label']}")
        if resp.get("discount_offer"):
            self.stdout.write(f"     🎁 {resp['discount_offer']['code']}")
        for item in judged:
            if not item.get("pass"):
                self.stdout.write(f"     ✗ {item['criterion']} — {item.get('note', '')}")
        for name, ok in results:
            if not ok:
                self.stdout.write(f"     ✗ check: {name}")

    def _code_checks(self, scenario, reply, cards, all_cards, resp):
        checks = scenario["checks"]
        results = []
        lang = scenario["lang"]
        texts = [reply] + [c["reason"] for c in cards]
        if lang == "ar":
            results.append(("arabic reply", all(ARABIC.search(t) for t in texts if t)))
        elif lang in ("en", "other", "latin"):
            results.append(("no arabic script", not any(ARABIC.search(t) for t in texts)))
        results.append(("no markdown", not MARKDOWN.search(reply)))
        if checks.get("single_script"):
            results.append(("no mixed scripts", not re.search(r"[A-Za-z]{3,}", reply)))
        results.append(("no stalling phrase", not STALL.search(reply)))
        if "card" in checks:
            results.append((f"card shown={checks['card']}", bool(cards) == checks["card"]))
        if "card_sku" in checks:
            results.append((f"card is {checks['card_sku']}", any(c["sku"] == checks["card_sku"] for c in cards)))
        if "never_sku" in checks:
            results.append((f"never shows {checks['never_sku']}", all(c["sku"] != checks["never_sku"] for c in all_cards)))
        if "discount" in checks:
            results.append((f"discount offered={checks['discount']}", bool(resp.get("discount_offer")) == checks["discount"]))
        if checks.get("no_tryon"):
            results.append(("no try-on button", all(c.get("tryon_supported") is False for c in cards)))
        if checks.get("no_repeat"):
            skus = [c["sku"] for c in all_cards]
            results.append(("no repeated card", len(skus) == len(set(skus))))
        return results

    def _judge(self, history, criteria):
        import openai

        convo = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history)
        client = openai.OpenAI(api_key=settings.OPENAI_API_KEY, timeout=60, max_retries=1)
        try:
            resp = client.responses.create(
                model=settings.OPENAI_MODEL,
                instructions=JUDGE_PROMPT,
                input=(
                    f"Store information the assistant had:\n{STORE_INFO}\n\n"
                    f"Active discount code the assistant may offer: WELCOME10 (10% off your first order)\n\n"
                    f"Catalog the assistant had (in-stock only unless noted):\n"
                    f"{json.dumps(CATALOG, ensure_ascii=False)}\n\n"
                    f"Conversation:\n{convo}\n\nCriteria:\n" + "\n".join(f"- {c}" for c in criteria)
                ),
                reasoning={"effort": "medium"},
                max_output_tokens=3000,
                store=False,
            )
            text = resp.output_text.strip()
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
            return data.get("results") or [], data.get("persuasion")
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"     judge failed: {exc}"))
            return [], None
