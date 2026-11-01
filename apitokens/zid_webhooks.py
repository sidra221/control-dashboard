import logging
import threading

from django.db import connection as db_connection

from .models import ZidConnection

logger = logging.getLogger(__name__)


def start_initial_sync(connection_pk):
    """أول مزامنة بعد التثبيت بالخلفية حتى ما يستنى التاجر."""
    threading.Thread(target=_initial_sync, args=(connection_pk,), daemon=True).start()


def _initial_sync(connection_pk):
    try:
        from .services import catalog as catalog_service

        connection = ZidConnection.objects.select_related("merchant").get(pk=connection_pk)
        sync = getattr(catalog_service, "sync_zid", None)
        if sync is None:
            return  # المزامنة بتنضاف بتاسك 3
        count = sync(connection.merchant, connection)
        logger.info("zid_initial_sync store=%s products=%s", connection.store_id, count)
    except Exception:  # noqa: BLE001 — خيط بالخلفية، أي خطأ لازم ينسجّل بس
        logger.exception("zid_initial_sync_failed connection=%s", connection_pk)
    finally:
        db_connection.close()
