"""اختبارات أمر purge_old_data: حذف بيانات الزبائن الأقدم من مدة الاحتفاظ."""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (
    ApiToken, Conversation, DiscountCode, LlmUsage, MerchantAccount, PhoneOtp, Product, TryOnEvent,
)


@override_settings(DATA_RETENTION_DAYS=365)
class PurgeOldDataTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.merchant = ApiToken.objects.create(label="M", token="PURGETOKEN001", image_quota=5)
        self.product = Product.objects.create(
            merchant=self.merchant, sku="SKU-1", title="Dress", price="10",
            image_url="https://example.com/d.jpg",
        )
        self.discount = DiscountCode.objects.create(merchant=self.merchant, code="SAVE10")
        self.verified = MerchantAccount.objects.create(
            merchant=self.merchant, name="V", phone="+966500000001", password="x", phone_verified=True,
        )
        self.unverified = MerchantAccount.objects.create(name="U", phone="+966500000002", password="x")

    def ago(self, days):
        return self.now - timedelta(days=days)

    def conversation(self, session_id, started_days, updated_days):
        conv = Conversation.objects.create(merchant=self.merchant, session_id=session_id)
        Conversation.objects.filter(pk=conv.pk).update(
            started_at=self.ago(started_days), updated_at=self.ago(updated_days),
        )
        return conv

    def event(self, days):
        event = TryOnEvent.objects.create(merchant=self.merchant, sku="SKU-1", product=self.product)
        TryOnEvent.objects.filter(pk=event.pk).update(created_at=self.ago(days))
        return event

    def usage(self, days):
        return LlmUsage.objects.create(merchant=self.merchant, day=timezone.localdate(self.ago(days)), calls=1)

    def otp(self, account, days, consumed=True):
        otp = PhoneOtp.objects.create(
            account=account, phone=account.phone, code_hash="h",
            expires_at=self.ago(days) + timedelta(minutes=10) if consumed else self.now + timedelta(minutes=10),
            consumed_at=self.ago(days) if consumed else None,
        )
        PhoneOtp.objects.filter(pk=otp.pk).update(created_at=self.ago(days))
        return otp

    def run_purge(self, *args):
        out = StringIO()
        call_command("purge_old_data", *args, stdout=out)
        return out.getvalue()

    def test_old_rows_deleted_recent_kept(self):
        old_conv = self.conversation("old", 400, 380)
        new_conv = self.conversation("new", 10, 1)
        old_event, new_event = self.event(400), self.event(10)
        old_usage, new_usage = self.usage(400), self.usage(10)

        out = self.run_purge()

        self.assertFalse(Conversation.objects.filter(pk=old_conv.pk).exists())
        self.assertTrue(Conversation.objects.filter(pk=new_conv.pk).exists())
        self.assertFalse(TryOnEvent.objects.filter(pk=old_event.pk).exists())
        self.assertTrue(TryOnEvent.objects.filter(pk=new_event.pk).exists())
        self.assertFalse(LlmUsage.objects.filter(pk=old_usage.pk).exists())
        self.assertTrue(LlmUsage.objects.filter(pk=new_usage.pk).exists())
        self.assertIn("deleted 1 conversations", out)
        self.assertIn("deleted 1 try-on events", out)
        self.assertIn("deleted 1 chat usage rows", out)

    def test_conversation_judged_by_last_activity(self):
        # بدأت من سنتين بس آخر نشاط فيها من شهر: لسا ضمن المدة.
        long_running = self.conversation("long", 730, 30)
        self.run_purge()
        self.assertTrue(Conversation.objects.filter(pk=long_running.pk).exists())

    def test_dry_run_deletes_nothing(self):
        self.conversation("old", 400, 380)
        self.event(400)
        self.usage(400)
        self.otp(self.verified, 60)

        out = self.run_purge("--dry-run")

        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(TryOnEvent.objects.count(), 1)
        self.assertEqual(LlmUsage.objects.count(), 1)
        self.assertEqual(PhoneOtp.objects.count(), 1)
        self.assertIn("would delete 1 conversations", out)
        self.assertIn("would delete 1 verification codes", out)

    def test_otp_rule(self):
        old_used = self.otp(self.verified, 60)
        recent_used = self.otp(self.verified, 5)
        old_unverified = self.otp(self.unverified, 60)
        # غير مستخدم بس منتهي (أُنشئ من 60 يوم وصلاحيته 10 دقايق).
        old_expired = PhoneOtp.objects.create(
            account=self.verified, phone=self.verified.phone, code_hash="h", expires_at=self.ago(60),
        )
        PhoneOtp.objects.filter(pk=old_expired.pk).update(created_at=self.ago(60))

        out = self.run_purge()

        self.assertFalse(PhoneOtp.objects.filter(pk=old_used.pk).exists())
        self.assertFalse(PhoneOtp.objects.filter(pk=old_expired.pk).exists())
        self.assertTrue(PhoneOtp.objects.filter(pk=recent_used.pk).exists())
        self.assertTrue(PhoneOtp.objects.filter(pk=old_unverified.pk).exists())
        self.assertIn("deleted 2 verification codes", out)

    def test_nothing_else_deleted(self):
        self.conversation("old", 400, 380)
        self.event(400)
        self.usage(400)
        self.otp(self.verified, 60)

        self.run_purge()

        self.assertTrue(ApiToken.objects.filter(pk=self.merchant.pk).exists())
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())
        self.assertTrue(DiscountCode.objects.filter(pk=self.discount.pk).exists())
        self.assertEqual(MerchantAccount.objects.count(), 2)

    def test_setting_respected(self):
        conv = self.conversation("mid", 100, 90)
        event = self.event(90)
        usage = self.usage(90)

        self.run_purge()
        self.assertEqual(Conversation.objects.filter(pk=conv.pk).count(), 1)

        with override_settings(DATA_RETENTION_DAYS=60):
            self.run_purge()
        self.assertFalse(Conversation.objects.filter(pk=conv.pk).exists())
        self.assertFalse(TryOnEvent.objects.filter(pk=event.pk).exists())
        self.assertFalse(LlmUsage.objects.filter(pk=usage.pk).exists())

    def test_batches_cover_all_rows(self):
        for i in range(7):
            self.event(400)
        out = self.run_purge("--batch-size", "3")
        self.assertEqual(TryOnEvent.objects.count(), 0)
        self.assertIn("deleted 7 try-on events", out)
