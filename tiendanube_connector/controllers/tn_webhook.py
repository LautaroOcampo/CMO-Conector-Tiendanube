# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import json
import logging
import hmac
import hashlib

_logger = logging.getLogger(__name__)


class TNWebhookController(http.Controller):
        """Controlador para recibir webhooks de TiendaNube"""

        @http.route('/tiendanube/webhook/test', type='http', auth='public', methods=['GET', 'POST'], csrf=False)
        def tn_webhook_test(self, **kwargs):
            """Endpoint de prueba para verificar que el webhook está accesible"""
            _logger.info("🧪 Endpoint de prueba del webhook llamado")
            return request.make_response(
                json.dumps({
                    "status": "ok",
                    "message": "Webhook endpoint is accessible",
                    "method": request.httprequest.method,
                    "url": request.httprequest.url
                }),
                headers={'Content-Type': 'application/json'},
                status=200
            )

        @http.route('/tiendanube/webhook/order', type='http', auth='public', methods=['POST'], csrf=False)
        def tn_order_webhook(self, **kwargs):
            """
            Endpoint para recibir webhooks de órdenes de TiendaNube
            
            TiendaNube envía webhooks con el evento de creación/actualización de órdenes.
            Formato esperado según documentación de TiendaNube.
            """
            try:
                # Log inicial para confirmar que el endpoint está siendo llamado
                _logger.info("=" * 80)
                _logger.info("🔔 WEBHOOK ENDPOINT LLAMADO: /tiendanube/webhook/order")
                _logger.info("📋 Método HTTP: %s", request.httprequest.method)
                _logger.info("📋 URL completa: %s", request.httprequest.url)
                _logger.info("=" * 80)
                
                # Obtener el cuerpo del POST
                raw_data = request.httprequest.data
                if not raw_data:
                    _logger.warning("⚠️ Webhook recibido sin datos")
                    _logger.warning("📋 Headers: %s", dict(request.httprequest.headers))
                    return request.make_response(
                        json.dumps({"status": "error", "message": "No data received"}),
                        headers={'Content-Type': 'application/json'},
                        status=400
                    )

                # Decodificar datos
                try:
                    data_str = raw_data.decode('utf-8')
                    data = json.loads(data_str) if data_str else {}
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    _logger.error("❌ Error decodificando datos del webhook: %s", e)
                    return request.make_response(
                        json.dumps({"status": "error", "message": "Invalid JSON"}),
                        headers={'Content-Type': 'application/json'},
                        status=400
                    )

                # Log del webhook recibido (completo para debugging)
                _logger.info("=" * 80)
                _logger.info("🔔 WEBHOOK RECIBIDO DE TIENDANUBE")
                _logger.info("=" * 80)
                _logger.info("📋 Headers recibidos:")
                for header_name, header_value in request.httprequest.headers.items():
                    _logger.info("   %s: %s", header_name, header_value)
                _logger.debug("📋 Estructura completa del webhook (DEBUG): %s", json.dumps(data, indent=2, ensure_ascii=False))
                _logger.debug("📋 Raw data (primeros 500 chars): %s", raw_data[:500] if raw_data else 'None')

                # Validar firma/token del webhook (opcional pero recomendado)
                # TiendaNube puede enviar un header con firma HMAC
                webhook_secret = request.httprequest.headers.get('X-Tiendanube-Signature') or \
                            request.httprequest.headers.get('X-Shopify-Hmac-Sha256')  # Algunos sistemas usan este header
                
                # Obtener secret configurado (si existe)
                # Por ahora validamos solo la estructura básica del payload
                event_type = data.get('type') or data.get('event') or data.get('event_type') or 'order/created'
                
                _logger.info("📋 Tipo de evento detectado: %s", event_type)
                
                # Extraer ID de la orden
                # TiendaNube puede enviar el webhook en diferentes formatos:
                # 1. {"id": 123} - Solo el ID
                # 2. {"data": {"id": 123}} - ID dentro de data
                # 3. {"order": {"id": 123}} - ID dentro de order
                # 4. {"order_id": 123} - ID como order_id
                # 5. {"resource": "order", "id": 123} - ID directo con resource
                order_id = None
                
                # Intentar diferentes estructuras posibles
                if isinstance(data, dict):
                    # Formato 1: ID directo
                    order_id = data.get('id')
                    
                    # Formato 2: ID dentro de data
                    if not order_id and isinstance(data.get('data'), dict):
                        order_id = data.get('data', {}).get('id')
                    
                    # Formato 3: ID dentro de order
                    if not order_id and isinstance(data.get('order'), dict):
                        order_id = data.get('order', {}).get('id')
                    
                    # Formato 4: order_id como campo directo
                    if not order_id:
                        order_id = data.get('order_id')
                    
                    # Formato 5: ID dentro de resource
                    if not order_id and isinstance(data.get('resource'), dict):
                        order_id = data.get('resource', {}).get('id')
                    
                    # Formato 6: ID como string que necesita conversión
                    if not order_id:
                        order_id_str = data.get('id') or data.get('order_id')
                        if order_id_str and isinstance(order_id_str, str) and order_id_str.isdigit():
                            order_id = int(order_id_str)
                
                # Si aún no tenemos el ID, intentar desde el raw data
                if not order_id:
                    _logger.warning("⚠️ No se pudo extraer ID de orden desde JSON. Intentando desde raw data...")
                    # Buscar patrones numéricos en el raw data que podrían ser IDs de orden
                    import re
                    if raw_data:
                        # Buscar "id": número o "order_id": número
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
                        status=200  # 200 para evitar reenvíos
                    )
                
                _logger.info("✅ ID de orden extraído: %s (tipo: %s)", order_id, type(order_id).__name__)

                # Esta ruta es exclusiva de órdenes; no filtrar por texto del evento (TN puede enviar "created", etc.).
                _logger.info("📦 Procesando webhook de orden. Tipo declarado: %s", event_type)

                # IDENTIFICAR CONFIGURACIÓN: preferir store_id del payload (multi-tienda)
                all_configs = request.env['tn.config'].sudo().search([
                    ('access_token', '!=', False),
                    ('store_id', '!=', False),
                    ('webhook_enabled', '=', True)
                ])

                store_id_webhook = data.get('store_id')
                if store_id_webhook is None and isinstance(data.get('data'), dict):
                    store_id_webhook = data.get('data', {}).get('store_id')

                tn_config_id = None
                matched_config = request.env['tn.config']
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

                # FLUJO: Webhook + Polling + Idempotencia
                # 1) Guardar notificación siempre (auditoría y fallback).
                # 2) Si hay configuración, intentar procesar la orden ya (GET orden por ID + create/update idempotente).
                # 3) Si falla o no hay config, la orden la procesará el polling (cron o botón); idempotencia evita duplicados.
                try:
                    notification = request.env['tn.webhook.notification'].sudo().create_notification(
                        order_id=order_id,
                        event_type=event_type,
                        raw_data=data_str,
                        tn_config_id=tn_config_id
                    )
                    _logger.info(
                        "✅ Notificación de webhook guardada: Order ID %s, Event: %s, Status: %s (ID: %s)",
                        order_id, event_type, notification.status, notification.id
                    )

                    # Procesar la orden de inmediato si hay configuración (mismo flujo idempotente que el polling)
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
                                    webhook_secret=None
                                )
                                request.env['tn.webhook.notification'].sudo().mark_as_processed_for_order(
                                    order_id, config.company_id.id
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
                            "message": "Webhook notification saved" + (", order processed" if order_processed else ", will be processed by polling if needed"),
                            "order_id": order_id,
                            "event_type": event_type,
                            "notification_id": notification.id,
                            "order_processed": order_processed,
                        }),
                        headers={'Content-Type': 'application/json'},
                        status=200
                    )

                except Exception as e:
                    _logger.exception("❌ Error guardando notificación de webhook: %s", e)
                    # Retornar 200 para evitar que TiendaNube reenvíe el webhook
                    return request.make_response(
                        json.dumps({
                            "status": "error",
                            "message": str(e),
                            "error_type": type(e).__name__
                        }),
                        headers={'Content-Type': 'application/json'},
                        status=200  # 200 para evitar reenvíos
                    )

            except Exception as e:
                _logger.exception("❌ Error procesando webhook: %s", e)
                return request.make_response(
                    json.dumps({
                        "status": "error",
                        "message": "Internal server error"
                    }),
                    headers={'Content-Type': 'application/json'},
                    status=500
                )
    
