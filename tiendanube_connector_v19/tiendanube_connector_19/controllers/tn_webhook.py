# -*- coding: utf-8 -*-
import json
import logging
import re

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Parámetro ir.config_parameter: solo si es True (y usuario administrador) responde /tiendanube/webhook/test
WEBHOOK_TEST_ICP_KEY = 'tiendanube_connector.webhook_test_endpoint_enabled'


def _tn_webhook_test_endpoint_enabled(env):
    raw = env['ir.config_parameter'].sudo().get_param(WEBHOOK_TEST_ICP_KEY, 'False')
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def _tn_linkedstore_hmac_header(httprequest):
    """Cabecera HMAC de Nuvemshop/TiendaNube (Werkzeug normaliza el nombre)."""
    return (
        httprequest.headers.get('X-Linkedstore-Hmac-Sha256')
        or httprequest.environ.get('HTTP_X_LINKEDSTORE_HMAC_SHA256')
    )


class TNWebhookController(http.Controller):
    """Controlador para recibir webhooks de TiendaNube."""

    @http.route('/tiendanube/webhook/test', type='http', auth='user', methods=['GET', 'POST'], csrf=False)
    def tn_webhook_test(self, **kwargs):
        """Endpoint de prueba (solo administradores y si está habilitado en parámetros del sistema)."""
        if not (
            request.env.user.has_group('base.group_system')
            or request.env.user.has_group('tiendanube_connector.group_tn_admin')
        ):
            return request.make_response(
                json.dumps({
                    'status': 'error',
                    'message': (
                        'Forbidden: Settings / Administración or group '
                        '"Administrador TiendaNube" required.'
                    ),
                }),
                headers={'Content-Type': 'application/json'},
                status=403,
            )
        if not _tn_webhook_test_endpoint_enabled(request.env):
            return request.make_response(
                json.dumps({
                    'status': 'error',
                    'message': (
                        'Webhook test endpoint is disabled. '
                        'Enable it in Settings → TiendaNube → '
                        '"Enable webhook test endpoint" (admin only), or set '
                        f'ir.config_parameter {WEBHOOK_TEST_ICP_KEY}=True for development.'
                    ),
                }),
                headers={'Content-Type': 'application/json'},
                status=403,
            )
        _logger.info("🧪 Endpoint de prueba del webhook llamado (user=%s)", request.env.user.login)
        return request.make_response(
            json.dumps({
                "status": "ok",
                "message": "Webhook endpoint is accessible",
                "method": request.httprequest.method,
                "url": request.httprequest.url,
            }),
            headers={'Content-Type': 'application/json'},
            status=200,
        )

    @http.route('/tiendanube/webhook/order', type='http', auth='public', methods=['POST'], csrf=False)
    def tn_order_webhook(self, **kwargs):
        """
        Recibe webhooks de órdenes (p. ej. order/created, order/updated).

        Seguridad (endpoint ``auth='public'``): siempre se exige verificación HMAC.
        Debe existir al menos un ``tn.config`` activo (token, store_id, webhooks on)
        con ``client_secret``; la cabecera ``x-linkedstore-hmac-sha256`` debe coincidir con
        HMAC-SHA256(cuerpo POST **raw**, client_secret) en hexadecimal (API Nuvemshop).
        Sin secret o sin firma válida no se procesa nada.
        """
        try:
            _logger.info("=" * 80)
            _logger.info("🔔 WEBHOOK ENDPOINT LLAMADO: /tiendanube/webhook/order")
            _logger.info("📋 Método HTTP: %s", request.httprequest.method)
            _logger.info("📋 URL completa: %s", request.httprequest.url)
            _logger.info("=" * 80)

            raw_data = request.httprequest.data
            if not raw_data:
                _logger.warning("⚠️ Webhook recibido sin datos")
                _logger.warning("📋 Headers: %s", dict(request.httprequest.headers))
                return request.make_response(
                    json.dumps({"status": "error", "message": "No data received"}),
                    headers={'Content-Type': 'application/json'},
                    status=400,
                )

            try:
                data_str = raw_data.decode('utf-8')
                data = json.loads(data_str) if data_str else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                _logger.error("❌ Error decodificando datos del webhook: %s", e)
                return request.make_response(
                    json.dumps({"status": "error", "message": "Invalid JSON"}),
                    headers={'Content-Type': 'application/json'},
                    status=400,
                )

            if not isinstance(data, dict):
                data = {}

            store_id_webhook = data.get('store_id')
            if store_id_webhook is None and isinstance(data.get('data'), dict):
                store_id_webhook = data.get('data', {}).get('store_id')

            all_configs = request.env['tn.config'].sudo().search([
                ('access_token', '!=', False),
                ('store_id', '!=', False),
                ('webhook_enabled', '=', True),
            ])

            if not all_configs:
                _logger.warning(
                    "⚠️ Webhook rechazado: no hay tn.config con webhooks activos y token/store."
                )
                return request.make_response(
                    json.dumps({
                        "status": "error",
                        "message": (
                            "No active TiendaNube webhook configuration "
                            "(tn.config: access_token, store_id, webhook_enabled)."
                        ),
                    }),
                    headers={'Content-Type': 'application/json'},
                    status=503,
                )

            hmac_header = _tn_linkedstore_hmac_header(request.httprequest)
            TNConfig = request.env['tn.config'].sudo()

            if store_id_webhook is not None and str(store_id_webhook).strip() != '':
                hmac_candidates = all_configs.filtered(
                    lambda c: str(c.store_id).strip() == str(store_id_webhook).strip()
                )
                if not hmac_candidates:
                    hmac_candidates = all_configs
            else:
                hmac_candidates = all_configs

            with_secret = hmac_candidates.filtered(lambda c: bool(c.sudo().client_secret))
            if not with_secret:
                _logger.warning(
                    "⚠️ Webhook rechazado: ninguna tn.config candidata tiene Client Secret; "
                    "configure el secret de la app (mismo valor que usa TiendaNube para firmar el HMAC)."
                )
                return request.make_response(
                    json.dumps({
                        "status": "error",
                        "message": (
                            "Webhook authentication required: set Client Secret on tn.config "
                            "(TiendaNube signs the raw body with HMAC-SHA256 using this secret)."
                        ),
                    }),
                    headers={'Content-Type': 'application/json'},
                    status=401,
                )

            if not hmac_header:
                _logger.warning(
                    "⚠️ Webhook rechazado: falta cabecera x-linkedstore-hmac-sha256."
                )
                return request.make_response(
                    json.dumps({
                        "status": "error",
                        "message": "Missing x-linkedstore-hmac-sha256 header",
                    }),
                    headers={'Content-Type': 'application/json'},
                    status=401,
                )

            valid = any(
                TNConfig.verify_linkedstore_webhook_hmac(
                    raw_data, hmac_header, cfg.sudo().client_secret
                )
                for cfg in with_secret
            )
            if not valid:
                _logger.warning("⚠️ Webhook rechazado: firma HMAC inválida")
                return request.make_response(
                    json.dumps({
                        "status": "error",
                        "message": "Invalid webhook signature",
                    }),
                    headers={'Content-Type': 'application/json'},
                    status=401,
                )

            _logger.info("=" * 80)
            _logger.info("🔔 WEBHOOK RECIBIDO DE TIENDANUBE")
            _logger.info("=" * 80)
            _logger.info("📋 Headers recibidos:")
            for header_name, header_value in request.httprequest.headers.items():
                if header_name.lower() == 'x-linkedstore-hmac-sha256':
                    _logger.info("   %s: <redacted>", header_name)
                else:
                    _logger.info("   %s: %s", header_name, header_value)
            _logger.debug("📋 Estructura completa del webhook (DEBUG): %s", json.dumps(data, indent=2, ensure_ascii=False))
            _logger.debug("📋 Raw data (primeros 500 chars): %s", raw_data[:500] if raw_data else 'None')

            event_type = data.get('type') or data.get('event') or data.get('event_type') or 'order/created'
            _logger.info("📋 Tipo de evento detectado: %s", event_type)

            order_id = None
            if isinstance(data, dict):
                order_id = data.get('id')
                if not order_id and isinstance(data.get('data'), dict):
                    order_id = data.get('data', {}).get('id')
                if not order_id and isinstance(data.get('order'), dict):
                    order_id = data.get('order', {}).get('id')
                if not order_id:
                    order_id = data.get('order_id')
                if not order_id and isinstance(data.get('resource'), dict):
                    order_id = data.get('resource', {}).get('id')
                if not order_id:
                    order_id_str = data.get('id') or data.get('order_id')
                    if order_id_str and isinstance(order_id_str, str) and order_id_str.isdigit():
                        order_id = int(order_id_str)

            if not order_id:
                _logger.warning("⚠️ No se pudo extraer ID de orden desde JSON. Intentando desde raw data...")
                if raw_data:
                    matches = re.findall(r'["\']?(?:id|order_id)["\']?\s*:\s*(\d+)', data_str, re.IGNORECASE)
                    if matches:
                        order_id = int(matches[0])
                        _logger.info("✅ ID de orden extraído desde raw data: %s", order_id)

            if not order_id:
                _logger.error("❌ Webhook sin ID de orden. Estructura completa recibida:")
                _logger.error("   JSON: %s", json.dumps(data, indent=2, ensure_ascii=False))
                _logger.error("   Raw (primeros 1000 chars): %s", raw_data[:1000] if raw_data else 'None')
                return request.make_response(
                    json.dumps({"status": "error", "message": "No order ID found in webhook payload"}),
                    headers={'Content-Type': 'application/json'},
                    status=200,
                )

            _logger.info("✅ ID de orden extraído: %s (tipo: %s)", order_id, type(order_id).__name__)
            _logger.info("📦 Procesando webhook de orden. Tipo declarado: %s", event_type)

            matched_config = request.env['tn.config']
            tn_config_id = None
            if all_configs:
                if store_id_webhook is not None and str(store_id_webhook).strip() != '':
                    matched = all_configs.filtered(
                        lambda c: str(c.store_id).strip() == str(store_id_webhook).strip()
                    )
                    if len(matched) == 1:
                        matched_config = matched[0]
                    elif matched:
                        matched_config = matched[0]
                        _logger.warning(
                            "⚠️ Varias configs con store_id %s; se usa la primera (ID %s)",
                            store_id_webhook, matched_config.id,
                        )
                    else:
                        matched_config = all_configs[0]
                        _logger.warning(
                            "⚠️ Webhook store_id=%s no coincide con ninguna config; "
                            "usando primera config (store_id Odoo=%s)",
                            store_id_webhook, matched_config.store_id,
                        )
                else:
                    matched_config = all_configs[0]
                    _logger.info(
                        "📋 Webhook sin store_id en payload; usando primera config (Store ID %s)",
                        matched_config.store_id,
                    )
                tn_config_id = matched_config.id
                _logger.info("📋 Configuración elegida: Store ID %s (ID: %s)", matched_config.store_id, tn_config_id)
            else:
                _logger.warning("⚠️ No se encontraron configuraciones activas. Guardando notificación sin configuración.")

            try:
                notification = request.env['tn.webhook.notification'].sudo().create_notification(
                    order_id=order_id,
                    event_type=event_type,
                    raw_data=data_str,
                    tn_config_id=tn_config_id,
                )
                _logger.info(
                    "✅ Notificación de webhook guardada: Order ID %s, Event: %s, Status: %s (ID: %s)",
                    order_id, event_type, notification.status, notification.id,
                )

                order_processed = False
                if tn_config_id and matched_config:
                    config = matched_config
                    try:
                        sync_service = request.env['tn.sync.service'].sudo()
                        order_data = sync_service._get_order_by_id(order_id, tn_config=config)
                        if order_data:
                            ctx = {
                                'tn_config_id': config.id,
                                'allowed_company_ids': [config.company_id.id],
                            }
                            request.env['tn.sale.order'].sudo().with_context(**ctx).create_order_from_webhook(
                                order_data,
                                webhook_secret=None,
                            )
                            request.env['tn.webhook.notification'].sudo().mark_as_processed_for_order(
                                order_id, config.company_id.id,
                            )
                            order_processed = True
                            _logger.info("✅ Orden TN %s creada/actualizada desde webhook (idempotente)", order_id)
                        else:
                            _logger.warning(
                                "⚠️ Webhook: API no devolvió datos de orden %s (token/store?). "
                                "Revisar config Store ID %s.",
                                order_id, config.store_id,
                            )
                    except Exception:
                        _logger.exception(
                            "❌ Webhook: error procesando orden TN %s (el cron/polling puede reintentar). "
                            "Causa arriba en el traceback.",
                            order_id,
                        )

                return request.make_response(
                    json.dumps({
                        "status": "ok",
                        "message": "Webhook notification saved" + (
                            ", order processed" if order_processed else ", will be processed by polling if needed"
                        ),
                        "order_id": order_id,
                        "event_type": event_type,
                        "notification_id": notification.id,
                        "order_processed": order_processed,
                    }),
                    headers={'Content-Type': 'application/json'},
                    status=200,
                )

            except Exception as e:
                _logger.exception("❌ Error guardando notificación de webhook: %s", e)
                return request.make_response(
                    json.dumps({
                        "status": "error",
                        "message": str(e),
                        "error_type": type(e).__name__,
                    }),
                    headers={'Content-Type': 'application/json'},
                    status=200,
                )

        except Exception:
            _logger.exception("❌ Error procesando webhook")
            return request.make_response(
                json.dumps({"status": "error", "message": "Internal server error"}),
                headers={'Content-Type': 'application/json'},
                status=500,
            )
