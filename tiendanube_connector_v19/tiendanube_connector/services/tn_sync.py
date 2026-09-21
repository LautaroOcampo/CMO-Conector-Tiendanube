# -*- coding: utf-8 -*-
"""
Servicio de sincronización con TiendaNube
Maneja todas las operaciones de API con TiendaNube
"""
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging
import requests
import base64
from datetime import datetime
import time
import os
import re
import json

_logger = logging.getLogger(__name__)


class TNSyncService(models.Model):
    """Servicio para sincronizar con TiendaNube"""
    _name = 'tn.sync.service'
    _description = 'Servicio de Sincronización TiendaNube'

    def _set_product_published(self, tn_product_id, published, tn_config=None):
        """
        Cambia solo el estado 'published' de un producto en TiendaNube, sin tocar
        nombre, descripción ni variantes.

        Se usa para pausar/activar productos desde Odoo sin riesgo de renombrarlos
        cuando se trabaja a nivel variante.
        """
        if not tn_product_id:
            raise UserError(_('No se puede cambiar el estado publicado sin un ID de producto TiendaNube.'))

        # Validar configuración (usa misma lógica que export_publication)
        config = self._get_config(tn_config=tn_config)
        store_id = config.get('store_id', '').strip()
        if not store_id:
            raise UserError(_('Debe configurar el Store ID en la cuenta de TiendaNube'))
        if not config.get('access_token'):
            raise UserError(_('Debe configurar el Access Token en la cuenta de TiendaNube'))

        endpoint = f'/products/{tn_product_id}'
        payload = {
            'published': bool(published),
        }
        _logger.info("📤 Cambiando estado 'published' en TiendaNube: product_id=%s, published=%s", tn_product_id, bool(published))
        Pub = self.env['tn.publication'].sudo()
        domain = [('tn_product_id', '=', str(tn_product_id))]
        if tn_config:
            domain.append(('tn_config_id', '=', tn_config.id))
        publication = Pub.search(domain, limit=1)
        start = time.time()
        try:
            self._make_request('PUT', endpoint, data=payload, tn_config=tn_config)
        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            self._tn_audit_sync(
                publication,
                'status_change',
                'error',
                tn_config=tn_config,
                value_before=str(not bool(published)),
                value_after=str(bool(published)),
                http_status=0,
                duration_ms=duration_ms,
                error_message=(str(e) or '')[:65535],
                force_log=True,
            )
            raise
        duration_ms = int((time.time() - start) * 1000)
        self._tn_audit_sync(
            publication,
            'status_change',
            'ok',
            tn_config=tn_config,
            value_before=str(not bool(published)),
            value_after=str(bool(published)),
            http_status=200,
            duration_ms=duration_ms,
            force_log=True,
        )

    def get_product_published(self, tn_product_id, tn_config=None):
        """
        Obtiene el estado 'published' de un producto en TiendaNube (GET /products/{id}).
        Útil para actualizar Odoo cuando el producto se pausa/activa desde TN.
        """
        try:
            response = self._make_request('GET', f'/products/{tn_product_id}', retry=False, tn_config=tn_config)
            if response and isinstance(response, dict):
                return bool(response.get('published', False))
        except Exception as e:
            _logger.warning("⚠️ No se pudo obtener estado published para producto %s: %s", tn_product_id, e)
        return None

    def _tn_audit_sync(
        self,
        publication,
        operation,
        result,
        tn_config=None,
        value_before=None,
        value_after=None,
        http_status=None,
        duration_ms=None,
        error_message=None,
        force_log=False,
    ):
        """Auditoría tn.sync.log; no debe interrumpir la sincronización."""
        Log = self.env['tn.sync.log'].sudo()
        config = tn_config
        if not config and publication:
            config = publication.tn_config_id
        pub_id = publication.id if publication else False
        Log._log_sync(
            config,
            operation,
            result,
            publication_id=pub_id or False,
            value_before=value_before,
            value_after=value_after,
            http_status=http_status,
            duration_ms=duration_ms,
            error_message=error_message,
            trigger='manual' if self.env.context.get('tn_sync_manual') else 'auto',
            force_log=force_log,
        )

    def _extract_tn_product_id(self, response):
        """Saca el ID de producto de la respuesta de TiendaNube (varios formatos)."""
        _logger.info("[TN BIND] extract input type=%s", type(response).__name__)
        if not response:
            _logger.warning("[TN BIND] extract: response vacía")
            return None
        if isinstance(response, list) and response:
            _logger.info("[TN BIND] extract: lista len=%s, usando primer elemento", len(response))
            return self._extract_tn_product_id(response[0])
        if not isinstance(response, dict):
            try:
                return int(response)
            except (TypeError, ValueError):
                _logger.warning("[TN BIND] extract: no pude convertir %r a int", response)
                return None
        raw = response.get('id') or response.get('product_id')
        product = response.get('product')
        if raw in (None, False, '', 0, '0') and isinstance(product, dict):
            raw = product.get('id')
        data = response.get('data')
        if raw in (None, False, '', 0, '0') and isinstance(data, dict):
            raw = data.get('id')
        loc = response.get('_http_location') or response.get('location')
        if raw in (None, False, '', 0, '0') and loc:
            loc_match = re.search(r'/products/(\d+)', str(loc))
            if loc_match:
                raw = loc_match.group(1)
                _logger.info("[TN BIND] extract: id desde location=%s", raw)
        if raw in (None, False, '', 0, '0'):
            try:
                snippet = json.dumps(response, ensure_ascii=False)[:1500]
            except Exception:
                snippet = str(response)[:1500]
            _logger.warning("[TN BIND] extract: no hay id. keys=%s body=%s", list(response.keys()), snippet)
            return None
        try:
            parsed = int(raw)
            _logger.info("[TN BIND] extract: tn_product_id=%s", parsed)
            return parsed
        except (TypeError, ValueError):
            _logger.warning("[TN BIND] extract: id no numérico %r", raw)
            return None

    def _lookup_tn_product_id_by_handle(self, handle, tn_config=None):
        """Si el POST no devolvió id, intenta encontrarlo por handle."""
        handle = (handle or '').strip()
        if not handle:
            return None
        try:
            _logger.info("[TN BIND] lookup GET /products?handle=%s", handle)
            response = self._make_request(
                'GET', '/products', params={'handle': handle, 'per_page': 50},
                retry=False, tn_config=tn_config,
            )
            products = response if isinstance(response, list) else (
                (response or {}).get('products') or (response or {}).get('data') or []
            )
            if isinstance(response, dict) and response.get('id'):
                products = [response]
            if not isinstance(products, list):
                return None
            handle_l = handle.lower()
            for prod in products:
                if not isinstance(prod, dict):
                    continue
                prod_handle = prod.get('handle')
                if isinstance(prod_handle, dict):
                    prod_handle = prod_handle.get('es') or next((v for v in prod_handle.values() if v), '')
                if str(prod_handle or '').strip().lower() == handle_l:
                    found = self._extract_tn_product_id(prod)
                    _logger.info("[TN BIND] lookup match handle=%s id=%s", handle, found)
                    return found
            _logger.warning("[TN BIND] lookup: ningún producto con handle=%s (n=%s)", handle, len(products))
        except Exception:
            _logger.exception("[TN BIND] lookup por handle falló")
        return None

    def _bind_publication_to_tn_product(self, publication, product_id, response=None):
        """Graba el ID de TiendaNube en la publicación de Odoo (ORM + SQL + commit)."""
        _logger.info(
            "[TN BIND] start odoo_pub=%s product_id=%s exists=%s",
            publication.id if publication else None,
            product_id,
            bool(publication and publication.exists()),
        )
        if not publication or not publication.id or not product_id:
            _logger.error("[TN BIND] abort: publication o product_id faltante")
            return False
        pid = int(product_id)
        pub_id = int(publication.id)
        now = fields.Datetime.now()
        try:
            publication.sudo().write({
                'tn_product_id': pid,
                'sync_state': 'synced',
                'sync_error_message': False,
                'last_sync_date': now,
            })
            publication.flush_recordset(['tn_product_id', 'sync_state', 'last_sync_date', 'sync_error_message'])
            _logger.info("[TN BIND] ORM write OK pub=%s tn_product_id=%s", pub_id, pid)
        except Exception:
            _logger.exception("[TN BIND] ORM write falló pub=%s tn_id=%s", pub_id, pid)
        self.env.cr.execute(
            """
            UPDATE tn_publication
               SET tn_product_id = %s,
                   sync_state = 'synced',
                   last_sync_date = %s
             WHERE id = %s
            """,
            (pid, now, pub_id),
        )
        _logger.info("[TN BIND] SQL UPDATE rowcount=%s pub=%s tn_id=%s", self.env.cr.rowcount, pub_id, pid)
        self.env.cr.execute(
            "SELECT id, tn_product_id, sync_state FROM tn_publication WHERE id = %s",
            (pub_id,),
        )
        row = self.env.cr.fetchone()
        _logger.info("[TN BIND] SQL verify row=%s", row)
        try:
            self.env.cr.commit()
            _logger.info("[TN BIND] commit OK pub=%s tn_product_id=%s", pub_id, pid)
        except Exception:
            _logger.exception("[TN BIND] commit falló pub=%s", pub_id)
        publication.invalidate_recordset(['tn_product_id', 'sync_state', 'last_sync_date'])
        _logger.info("[TN BIND] ORM after commit tn_product_id=%s", publication.sudo().tn_product_id)
        return bool(publication.sudo().tn_product_id)

    def _get_config(self, tn_config=None):
        """
        Obtiene la configuración de TiendaNube
        
        Args:
            tn_config: Registro de tn.config a usar. Si se proporciona, se usa esta configuración
                       en lugar de la global desde ir.config_parameter
        
        Returns:
            dict: Configuración de TiendaNube
        """
        # Si se proporciona una configuración específica, usarla
        if tn_config:
            cfg = tn_config.sudo()
            return {
                'client_id': cfg.client_id or '',
                'client_secret': cfg.client_secret or '',
                'access_token': cfg.access_token or '',
                'refresh_token': cfg.refresh_token or '',
                'store_id': cfg.store_id or '',
                'api_base_url': cfg.api_base_url or 'https://api.tiendanube.com/v1',
                'store_url': '',  # Legacy, no usado en tn.config
            }
        
        # Fallback a configuración global desde ir.config_parameter
        ICP = self.env['ir.config_parameter'].sudo()
        return {
            'client_id': ICP.get_param('tiendanube_connector.client_id', ''),
            'client_secret': ICP.get_param('tiendanube_connector.client_secret', ''),
            'access_token': ICP.get_param('tiendanube_connector.access_token', ''),
            'refresh_token': ICP.get_param('tiendanube_connector.refresh_token', ''),
            'store_id': ICP.get_param('tiendanube_connector.store_id', ''),
            'api_base_url': ICP.get_param('tiendanube_connector.api_base_url', 'https://api.tiendanube.com/v1'),
            'store_url': ICP.get_param('tiendanube_connector.store_url', ''),  # Legacy, para compatibilidad
        }

    def _get_base_url(self, tn_config=None):
        """
        Construye la URL base de la API
        
        Args:
            tn_config: Registro de tn.config a usar (opcional)
        """
        config = self._get_config(tn_config=tn_config)
        api_base_url = config.get('api_base_url', 'https://api.tiendanube.com/v1').strip()
        store_id = config.get('store_id', '').strip()
        
        # Si hay store_id, construir URL con store_id
        if store_id:
            # Normalizar api_base_url
            api_base_url = api_base_url.rstrip('/')
            # Construir URL: https://api.tiendanube.com/v1/{store_id}
            return f"{api_base_url}/{store_id}"
        
        # Fallback a store_url (legacy)
        store_url = config.get('store_url', '').strip()
        if store_url:
            store_url = store_url.replace('http://', '').replace('https://', '').strip('/')
            return f"https://{store_url}/api"
        
        raise UserError(_('Debe configurar el Store ID o la URL de la tienda en Configuración'))

    def _get_headers(self, include_content_type=True, tn_config=None):
        """
        Obtiene los headers para las peticiones
        
        Args:
            include_content_type: Si incluir Content-Type en los headers
            tn_config: Registro de tn.config a usar (opcional)
        """
        config = self._get_config(tn_config=tn_config)
        access_token = config.get('access_token', '')
        
        if not access_token:
            raise UserError(_('Debe configurar el Access Token en Configuración'))
        
        headers = {
            'Authentication': f'bearer {access_token}',  # Formato requerido por TiendaNube
            'User-Agent': 'Conector Odoo-TiendaNube/1.0',  # User-Agent descriptivo
        }
        
        if include_content_type:
            headers['Content-Type'] = 'application/json'
        
        return headers

    def _make_request(self, method, endpoint, data=None, params=None, files=None, retry=True, tn_config=None):
        """
        Realiza una petición HTTP a la API de TiendaNube
        Maneja errores, reintentos y límites de rate
        
        Args:
            method: Método HTTP (GET, POST, PUT, DELETE)
            endpoint: Endpoint de la API (sin la base URL)
            data: Datos a enviar (dict)
            params: Parámetros de query string (dict)
            files: Archivos para multipart/form-data
            retry: Si reintentar en caso de error
            tn_config: Registro de tn.config a usar (opcional)
        """
        base_url = self._get_base_url(tn_config=tn_config)
        url = f"{base_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers(include_content_type=(files is None), tn_config=tn_config)
        
        timeout_seconds = 30
        if tn_config and getattr(tn_config, 'api_timeout_seconds', None):
            timeout_seconds = max(5, min(tn_config.api_timeout_seconds, 120))
        max_retries = 3
        if tn_config and getattr(tn_config, 'api_max_retries', None):
            max_retries = max(1, min(tn_config.api_max_retries, 10))
        retry_count = 0
        max_total_seconds = 120  # tope total para no quedarse colgado
        start_time = time.time()
        
        while retry_count < max_retries:
            try:
                if time.time() - start_time > max_total_seconds:
                    raise UserError(_('Tiempo máximo total de espera (%d s) superado') % max_total_seconds)
                _logger.info("🌐 TiendaNube API Request: %s %s", method, url)
                
                kwargs = {
                    'headers': headers,
                    'timeout': timeout_seconds,
                }
                
                if params:
                    kwargs['params'] = params
                
                if files:
                    # Multipart/form-data: pasar files y data juntos
                    kwargs['files'] = files
                    if data:
                        # En multipart, data va como form fields, no como JSON
                        kwargs['data'] = data
                        _logger.debug("📤 JSON Payload (multipart): %s", json.dumps(data, indent=2, ensure_ascii=False))
                elif data:
                    # JSON: solo pasar data como JSON; payload completo solo en DEBUG
                    try:
                        json_str = json.dumps(data, indent=2, ensure_ascii=False)
                        _logger.debug("📤 JSON Payload completo: %s", json_str)
                    except Exception as e:
                        _logger.warning("⚠️ No se pudo serializar JSON para log: %s", e)
                        _logger.debug("📤 JSON Payload (raw): %s", str(data))
                    kwargs['json'] = data
                
                response = requests.request(method, url, **kwargs)
                
                # Log de respuesta para debugging (especialmente útil para cuentas test)
                _logger.info("📥 TiendaNube API Response: Status %s", response.status_code)
                if response.status_code not in [200, 201, 204]:
                    _logger.warning("⚠️ Respuesta con error: Status %s", response.status_code)
                    try:
                        error_body = response.text[:500]  # Primeros 500 caracteres
                        _logger.warning("   Error body: %s", error_body)
                    except:
                        pass
                
                # Manejar rate limiting (429)
                # Límites API TiendaNube: 2 req/seg (120/min) por defecto; plan Next: 20 req/seg (1200/min).
                # Headers: x-rate-limit-limit, x-rate-limit-remaining, x-rate-limit-reset
                if response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', 2))
                    _logger.warning("⏳ Rate limit alcanzado. Esperando %d segundos...", retry_after)
                    time.sleep(retry_after)
                    retry_count += 1
                    continue
                
                # Manejar errores de autenticación (401)
                if response.status_code == 401:
                    config = self._get_config()
                    refresh_token = config.get('refresh_token', '').strip()
                    
                    # Si no hay refresh_token, no se puede renovar automáticamente
                    if not refresh_token:
                        _logger.error("❌ Access Token inválido o expirado (no hay Refresh Token para renovarlo)")
                        raise UserError(_(
                            'Access Token inválido o expirado.\n\n'
                            'Tu aplicación no usa Refresh Token, por lo que debes obtener un nuevo Access Token manualmente:\n'
                            '1. Ve al panel de socios tecnológicos de TiendaNube\n'
                            '2. Genera un nuevo Access Token\n'
                            '3. Reemplázalo en la Configuración de TiendaNube en Odoo'
                        ))
                    
                    # Si hay refresh_token, intentar renovar (solo si retry está habilitado)
                    if retry:
                        _logger.warning("🔑 Token expirado. Intentando refrescar con Refresh Token...")
                        if self.refresh_access_token().get('success'):
                            headers = self._get_headers(include_content_type=(files is None))
                            retry_count += 1
                            continue
                        else:
                            raise UserError(_('Access Token expirado y no se pudo renovar. Verifica el Refresh Token o reemplaza el Access Token manualmente.'))
                    else:
                        raise UserError(_('Access Token inválido o expirado. Verifica tus credenciales.'))
                
                # Verificar respuesta exitosa
                if response.status_code in [200, 201, 204]:
                    loc = response.headers.get('Location') or response.headers.get('location') or ''
                    _logger.info(
                        "[TN EXPORT] HTTP %s %s status=%s location=%s bytes=%s",
                        method, endpoint, response.status_code, loc or '-',
                        len(response.content or b''),
                    )
                    body = None
                    if response.content:
                        try:
                            body = response.json()
                        except ValueError:
                            _logger.warning(
                                "[TN EXPORT] JSON inválido en %s %s: %s",
                                method, endpoint, (response.text or '')[:2000],
                            )
                            body = {'success': True, 'data': response.text}
                    else:
                        body = {'success': True}
                    if isinstance(body, dict):
                        if not body.get('id') and loc:
                            loc_match = re.search(r'/products/(\d+)', str(loc))
                            if loc_match:
                                body['id'] = int(loc_match.group(1))
                                _logger.info("[TN EXPORT] ID tomado de header Location: %s", body['id'])
                        _logger.info(
                            "[TN EXPORT] respuesta keys=%s id=%s product_id=%s",
                            list(body.keys())[:30],
                            body.get('id'),
                            body.get('product_id'),
                        )
                    elif isinstance(body, list):
                        _logger.info("[TN EXPORT] respuesta list len=%s", len(body))
                    return body
                
                # Manejar errores - LOGGING DETALLADO
                _logger.error("=" * 100)
                _logger.error("❌ ERROR EN PETICIÓN TIENDANUBE")
                _logger.error("=" * 100)
                _logger.error("📋 Información del Request:")
                _logger.error("   Método: %s", method)
                _logger.error("   URL: %s", url)
                _logger.error("   Endpoint: %s", endpoint)
                _logger.error("   Status Code: %s", response.status_code)
                _logger.error("   Headers Request: %s", dict(headers))
                
                # Log del payload que se envió (si existe)
                if data:
                    try:
                        payload_str = json.dumps(data, indent=2, ensure_ascii=False) if isinstance(data, dict) else str(data)
                        _logger.error("📤 Payload enviado (completo):")
                        _logger.error("=" * 100)
                        _logger.error(payload_str)
                        _logger.error("=" * 100)
                    except Exception as e:
                        _logger.error("📤 Payload enviado (raw): %s", str(data))
                        _logger.error("⚠️ No se pudo formatear payload: %s", e)
                
                # Intentar parsear respuesta de error
                error_msg = f"Error {response.status_code}"
                error_details = None
                error_code = None
                error_description = None
                
                try:
                    error_data = response.json()
                    _logger.error("📥 Respuesta de error (JSON parseado):")
                    _logger.error("   %s", json.dumps(error_data, indent=2, ensure_ascii=False))
                    
                    # Intentar extraer información estructurada del error
                    if isinstance(error_data, dict):
                        error_code = error_data.get('code')
                        error_msg = error_data.get('message', error_msg)
                        error_description = error_data.get('description')
                        error_details = error_data
                        
                        # Si hay errores de validación específicos, mostrarlos
                        if 'errors' in error_data:
                            _logger.error("🔍 Errores de validación:")
                            for field, field_errors in error_data['errors'].items():
                                _logger.error("   Campo '%s': %s", field, field_errors)
                        
                        # Si hay información adicional en el error
                        for key, value in error_data.items():
                            if key not in ['code', 'message', 'description', 'errors']:
                                _logger.error("   %s: %s", key, value)
                    else:
                        error_details = error_data
                except Exception as e:
                    # Si no es JSON, intentar leer como texto
                    response_text = response.text if response.text else ''
                    error_msg = response_text[:500] if response_text else error_msg
                    error_details = response_text
                    _logger.error("⚠️ No se pudo parsear respuesta como JSON: %s", e)
                    _logger.error("📥 Respuesta de error (texto):")
                    _logger.error("   %s", response_text[:2000] if response_text else '(vacío)')
                
                # Log completo de la respuesta
                _logger.error("📥 Headers de respuesta:")
                _logger.error("   %s", json.dumps(dict(response.headers), indent=2, ensure_ascii=False))
                
                _logger.error("📥 Body completo de respuesta:")
                _logger.error("   %s", response.text[:2000] if response.text else '(vacío)')
                
                # Construir mensaje de error detallado
                detailed_error = f"Error {response.status_code}"
                if error_code:
                    detailed_error += f" (Código: {error_code})"
                if error_msg and error_msg != f"Error {response.status_code}":
                    detailed_error += f": {error_msg}"
                if error_description:
                    detailed_error += f" - {error_description}"
                
                _logger.error("=" * 100)
                _logger.error("❌ RESUMEN DEL ERROR: %s", detailed_error)
                _logger.error("=" * 100)
                
                raise UserError(_('Error en API TiendaNube: %s') % detailed_error)
                
            except requests.exceptions.Timeout:
                retry_count += 1
                _logger.error("=" * 100)
                _logger.error("⏱️ TIMEOUT EN PETICIÓN TIENDANUBE")
                _logger.error("=" * 100)
                _logger.error("📋 Información del Request:")
                _logger.error("   Método: %s", method)
                _logger.error("   URL: %s", url)
                _logger.error("   Endpoint: %s", endpoint)
                _logger.error("   Intento: %d/%d", retry_count, max_retries)
                if data:
                    _logger.error("   Payload size: %d bytes", len(str(data)))
                _logger.error("=" * 100)
                
                if retry_count >= max_retries:
                    raise UserError(_('Timeout al conectar con TiendaNube después de %d intentos') % max_retries)
                _logger.warning("⏱️ Timeout. Reintentando... (%d/%d)", retry_count, max_retries)
                time.sleep(2 ** retry_count)  # Backoff exponencial
                
            except requests.exceptions.RequestException as e:
                retry_count += 1
                _logger.error("=" * 100)
                _logger.error("❌ ERROR DE CONEXIÓN CON TIENDANUBE")
                _logger.error("=" * 100)
                _logger.error("📋 Información del Request:")
                _logger.error("   Método: %s", method)
                _logger.error("   URL: %s", url)
                _logger.error("   Endpoint: %s", endpoint)
                _logger.error("   Tipo de error: %s", type(e).__name__)
                _logger.error("   Mensaje: %s", str(e))
                _logger.error("   Intento: %d/%d", retry_count, max_retries)
                _logger.exception("📚 Stack trace completo:")
                _logger.error("=" * 100)
                if retry_count >= max_retries:
                    raise UserError(_('Error de conexión con TiendaNube después de %d intentos: %s') % (max_retries, str(e)))
                _logger.warning("⏱️ Reintentando por error de conexión... (%d/%d)", retry_count, max_retries)
                time.sleep(2 ** retry_count)  # Backoff exponencial
        
        raise UserError(_('Error al realizar la petición después de %d intentos') % max_retries)

    def test_connection(self):
        """Prueba la conexión con TiendaNube haciendo un GET /products?page=1"""
        try:
            # Hacer un GET /products?page=1 para validar credenciales
            params = {'page': 1}
            response = self._make_request('GET', '/products', params=params)
            
            # Si la respuesta es exitosa, las credenciales son válidas
            return {
                'success': True,
                'message': _('Conexión exitosa. Credenciales válidas.'),
                'data': response
            }
        except Exception as e:
            _logger.exception("Error al probar conexión: %s", e)
            return {
                'success': False,
                'error': str(e)
            }

    def refresh_access_token(self):
        """Refresca el token de acceso usando el refresh token"""
        config = self._get_config()
        client_id = config.get('client_id')
        client_secret = config.get('client_secret')
        refresh_token = config.get('refresh_token')
        
        if not all([client_id, client_secret, refresh_token]):
            return {
                'success': False,
                'error': _('Faltan credenciales para refrescar el token')
            }
        
        try:
            # Endpoint de TiendaNube para refrescar token
            # Nota: Este endpoint puede variar según la versión de la API
            url = "https://www.tiendanube.com/apps/authorize/token"
            
            data = {
                'client_id': client_id,
                'client_secret': client_secret,
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token,
            }
            
            response = requests.post(url, data=data, timeout=30)
            
            if response.status_code == 200:
                token_data = response.json()
                
                # Actualizar tokens en configuración
                ICP = self.env['ir.config_parameter'].sudo()
                ICP.set_param('tiendanube_connector.access_token', token_data.get('access_token', ''))
                if token_data.get('refresh_token'):
                    ICP.set_param('tiendanube_connector.refresh_token', token_data.get('refresh_token', ''))
                
                return {
                    'success': True,
                    'access_token': token_data.get('access_token'),
                    'refresh_token': token_data.get('refresh_token'),
                }
            else:
                error_msg = response.text
                _logger.error("Error al refrescar token: %s - %s", response.status_code, error_msg)
                return {
                    'success': False,
                    'error': f"Error {response.status_code}: {error_msg}"
                }
        except Exception as e:
            _logger.exception("Error al refrescar token: %s", e)
            return {
                'success': False,
                'error': str(e)
            }

    def _parse_tn_product_list_response(self, response):
        """Normaliza la respuesta de GET /products a una lista de dicts."""
        if isinstance(response, list):
            return response
        if isinstance(response, dict):
            return response.get('products', []) or response.get('data', []) or []
        return []

    def _fetch_tn_product_list_page(self, page, per_page=50, tn_config=None):
        """Una página del listado de productos (sin detalle por ID)."""
        params = {'page': page, 'per_page': per_page}
        response = self._make_request('GET', '/products', params=params, tn_config=tn_config)
        return self._parse_tn_product_list_response(response)

    def _fetch_all_tn_products_list(self, tn_config=None, limit=None):
        """Recorre el catálogo TN por páginas (solo listado, sin GET /products/{id})."""
        all_products = []
        page = 1
        per_page = 50
        while True:
            if limit is not None and len(all_products) >= limit:
                break
            products = self._fetch_tn_product_list_page(page, per_page=per_page, tn_config=tn_config)
            if not products:
                break
            if limit is not None:
                remaining = limit - len(all_products)
                products = products[:remaining]
            all_products.extend(products)
            if len(products) < per_page:
                break
            page += 1
            time.sleep(0.5)
        return all_products

    def _get_existing_tn_product_ids(self, tn_config=None):
        """IDs de producto TN que ya tienen al menos una publicación en Odoo."""
        domain = [('tn_product_id', '!=', False)]
        if tn_config:
            domain.append(('tn_config_id', '=', tn_config.id))
        return set(self.env['tn.publication'].search(domain).mapped('tn_product_id'))

    def _normalize_tn_product_id(self, raw_id):
        if raw_id in (None, False, ''):
            return None
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            return raw_id

    def discover_new_tn_product_ids(self, tn_config=None):
        """
        Scan completo del catálogo TN y diff contra tn_product_id ya presentes en Odoo.
        Retorna lista ordenada de IDs TN nuevos (sin importar aún).
        """
        config_label = tn_config.name if tn_config else 'global'
        existing_ids = self._get_existing_tn_product_ids(tn_config=tn_config)
        all_products = self._fetch_all_tn_products_list(tn_config=tn_config)
        scan_ids = []
        seen = set()
        for product_data in all_products:
            tn_product_id = self._normalize_tn_product_id(product_data.get('id'))
            if not tn_product_id or tn_product_id in seen:
                continue
            seen.add(tn_product_id)
            scan_ids.append(tn_product_id)
        new_ids = [pid for pid in scan_ids if pid not in existing_ids]
        _logger.info(
            '🔍 Scan novedades TN [%s]: %d productos en TN, %d ya en Odoo, %d nuevos',
            config_label,
            len(scan_ids),
            len(existing_ids),
            len(new_ids),
        )
        return new_ids

    def import_single_tn_product_by_id(
        self,
        tn_product_id,
        tn_config=None,
        download_images=False,
    ):
        """
        Importa un producto TN por ID (scan diario de novedades).
        Retorna dict con imported, imported_count, skipped, errors.
        """
        config_label = tn_config.name if tn_config else 'global'
        errors = []
        if not tn_product_id:
            return {'imported': False, 'imported_count': 0, 'skipped': True, 'errors': errors}

        existing_ids = self._get_existing_tn_product_ids(tn_config=tn_config)
        pid = self._normalize_tn_product_id(tn_product_id)
        if pid in existing_ids:
            _logger.info(
                '🔍 Scan novedades TN [%s]: omitido TN %s (ya en Odoo)',
                config_label,
                pid,
            )
            return {'imported': False, 'imported_count': 0, 'skipped': True, 'errors': errors}

        try:
            publication = self._create_publication_from_tn_data(
                {'id': pid},
                tn_config=tn_config,
                download_images=download_images,
            )
        except Exception as e:
            errors.append(str(e))
            _logger.exception(
                '❌ Scan novedades TN [%s]: error importando TN %s: %s',
                config_label,
                pid,
                e,
            )
            return {'imported': False, 'imported_count': 0, 'skipped': False, 'errors': errors}

        if publication:
            return {
                'imported': True,
                'imported_count': 1,
                'skipped': False,
                'errors': errors,
                'publication': publication,
            }
        _logger.info(
            '🔍 Scan novedades TN [%s]: omitido TN %s (sin datos)',
            config_label,
            pid,
        )
        return {'imported': False, 'imported_count': 0, 'skipped': True, 'errors': errors}

    def _import_product_list(
        self,
        all_products,
        tn_config=None,
        skip_existing=True,
        download_images=True,
        commit_after_each=False,
        config_label='',
        page=None,
    ):
        """Procesa una lista de productos TN; opcionalmente confirma cada uno en BD."""
        existing_ids = self._get_existing_tn_product_ids(tn_config=tn_config) if skip_existing else set()
        total = len(all_products)
        publications = []
        imported_count = 0
        skipped_count = 0

        for index, product_data in enumerate(all_products, start=1):
            raw_id = product_data.get('id')
            try:
                tn_product_id = int(raw_id) if raw_id is not None else None
            except (TypeError, ValueError):
                tn_product_id = raw_id

            if skip_existing and tn_product_id in existing_ids:
                skipped_count += 1
                continue

            page_info = f"página {page}, " if page else ""
            _logger.info(
                "📥 Importación TiendaNube [%s]: %sproducto %d/%d — importando TN %s",
                config_label or 'global',
                page_info,
                index,
                total,
                tn_product_id,
            )
            publication = self._create_publication_from_tn_data(
                product_data,
                tn_config=tn_config,
                download_images=download_images,
            )
            if publication:
                publications.append(publication)
                imported_count += 1
                if tn_product_id:
                    existing_ids.add(tn_product_id)
                if commit_after_each:
                    self.env.cr.commit()

        return {
            'imported_count': imported_count,
            'skipped_count': skipped_count,
            'publications': publications,
        }

    def import_publications_batch(
        self,
        page=1,
        per_page=25,
        tn_config=None,
        skip_existing=True,
        download_images=False,
        commit_after_each=True,
    ):
        """Importa un lote (una página API). Pensado para cron en Odoo.sh."""
        config_label = tn_config.name if tn_config else 'global'
        products = self._fetch_tn_product_list_page(page, per_page=per_page, tn_config=tn_config)
        if not products:
            return {
                'success': True,
                'imported_count': 0,
                'skipped_count': 0,
                'products_in_page': 0,
                'last_page': True,
                'page': page,
            }

        result = self._import_product_list(
            products,
            tn_config=tn_config,
            skip_existing=skip_existing,
            download_images=download_images,
            commit_after_each=commit_after_each,
            config_label=config_label,
            page=page,
        )
        result.update({
            'success': True,
            'products_in_page': len(products),
            'last_page': len(products) < per_page,
            'page': page,
        })
        return result

    def import_publications(self, limit=None, tn_config=None, skip_existing=True, download_images=False):
        """
        Importa publicaciones desde TiendaNube (síncrono; puede superar el límite HTTP de Odoo.sh).

        Preferir import_publications_batch vía cron para catálogos grandes.
        """
        start_all = time.time()
        config_label = tn_config.name if tn_config else 'global'
        try:
            _logger.info(
                "📥 Importación TiendaNube [%s]: listando catálogo...",
                config_label,
            )
            all_products = self._fetch_all_tn_products_list(tn_config=tn_config, limit=limit)
            total = len(all_products)
            if not total:
                duration_ms = int((time.time() - start_all) * 1000)
                self._tn_audit_sync(
                    False,
                    'product_import',
                    'ok',
                    tn_config=tn_config,
                    value_before='0',
                    value_after='0',
                    http_status=200,
                    duration_ms=duration_ms,
                    force_log=True,
                )
                return {
                    'success': True,
                    'count': 0,
                    'imported_count': 0,
                    'skipped_count': 0,
                    'total': 0,
                    'publications': [],
                }

            existing_count = len(self._get_existing_tn_product_ids(tn_config=tn_config)) if skip_existing else 0
            _logger.info(
                "📥 Importación TiendaNube [%s]: %d productos en TN, %d ya en Odoo (omitir=%s)",
                config_label,
                total,
                existing_count,
                skip_existing,
            )

            batch_result = self._import_product_list(
                all_products,
                tn_config=tn_config,
                skip_existing=skip_existing,
                download_images=download_images,
                commit_after_each=True,
                config_label=config_label,
            )
            imported_count = batch_result['imported_count']
            skipped_count = batch_result['skipped_count']
            publications = batch_result['publications']

            duration_ms = int((time.time() - start_all) * 1000)
            _logger.info(
                "📥 Importación TiendaNube [%s]: completada — "
                "%d importados/actualizados, %d omitidos de %d, %d ms",
                config_label,
                imported_count,
                skipped_count,
                total,
                duration_ms,
            )
            self._tn_audit_sync(
                False,
                'product_import',
                'ok',
                tn_config=tn_config,
                value_before=str(skipped_count),
                value_after=str(imported_count),
                http_status=200,
                duration_ms=duration_ms,
                force_log=True,
            )
            return {
                'success': True,
                'count': imported_count,
                'imported_count': imported_count,
                'skipped_count': skipped_count,
                'total': total,
                'publications': publications,
            }
        except Exception as e:
            duration_ms = int((time.time() - start_all) * 1000)
            self._tn_audit_sync(
                False,
                'product_import',
                'error',
                tn_config=tn_config,
                value_before='',
                value_after='',
                http_status=0,
                duration_ms=duration_ms,
                error_message=(str(e) or '')[:65535],
                force_log=True,
            )
            _logger.exception("Error al importar publicaciones: %s", e)
            raise UserError(_('Error al importar publicaciones: %s') % str(e))

    def _extract_multilang_value(self, value, default=''):
        """Extrae el valor de un campo multiidioma (puede ser dict o string)"""
        if not value:
            return default
        if isinstance(value, dict):
            # Intentar obtener el valor en español primero, luego cualquier otro
            return value.get('es') or value.get('en') or list(value.values())[0] if value else default
        return str(value)
    
    def _import_tags(self, tags_data):
        """Importa tags y devuelve lista de IDs para Many2many"""
        if not tags_data:
            return [(5, 0, 0)]  # Eliminar todos
        
        tag_ids = []
        
        # Si es una lista
        if isinstance(tags_data, list):
            _logger.info("📋 Procesando tags (lista con %d elementos): %s", len(tags_data), tags_data)
            for tag_item in tags_data:
                if tag_item:
                    # Si es un dict multiidioma, extraer el valor
                    if isinstance(tag_item, dict):
                        tag_name = self._extract_multilang_value(tag_item, '')
                    else:
                        tag_name = str(tag_item).strip()
                    
                    if tag_name:
                        # Buscar o crear tag
                        tag_record = self.env['tn.publication.tag'].search([
                            ('name', '=', tag_name)
                        ], limit=1)
                        
                        if not tag_record:
                            tag_record = self.env['tn.publication.tag'].create({
                                'name': tag_name,
                                'tn_tag_id': str(tag_item.get('id', '')) if isinstance(tag_item, dict) else ''
                            })
                            _logger.info("✅ Tag creado: '%s' (ID: %s)", tag_name, tag_record.id)
                        else:
                            _logger.info("✅ Tag existente encontrado: '%s' (ID: %s)", tag_name, tag_record.id)
                        
                        tag_ids.append(tag_record.id)
            
            _logger.info("📋 Tags procesados: %d tags totales (IDs: %s)", len(tag_ids), tag_ids)
        
        # Si es un string, separar por comas
        elif isinstance(tags_data, str):
            tags_list = [t.strip() for t in tags_data.split(',') if t.strip()]
            for tag_name in tags_list:
                tag_record = self.env['tn.publication.tag'].search([
                    ('name', '=', tag_name)
                ], limit=1)
                
                if not tag_record:
                    tag_record = self.env['tn.publication.tag'].create({
                        'name': tag_name
                    })
                
                tag_ids.append(tag_record.id)
        
        # Si es un diccionario, extraer valores
        elif isinstance(tags_data, dict):
            for tag_item in tags_data.values():
                if tag_item:
                    tag_name = str(tag_item).strip()
                    if tag_name:
                        tag_record = self.env['tn.publication.tag'].search([
                            ('name', '=', tag_name)
                        ], limit=1)
                        
                        if not tag_record:
                            tag_record = self.env['tn.publication.tag'].create({
                                'name': tag_name
                            })
                        
                        tag_ids.append(tag_record.id)
        
        return [(6, 0, tag_ids)] if tag_ids else [(5, 0, 0)]
    
    def _import_categories(self, categories_data, tn_config=None):
        """Importa categorías y devuelve lista de IDs para Many2many
        
        Args:
            categories_data: Datos de categorías desde TiendaNube
            tn_config: Registro de tn.config a usar para las llamadas a la API
        """
        if not categories_data:
            return [(5, 0, 0)]  # Eliminar todos
        
        category_ids = []
        
        # Si es una lista
        if isinstance(categories_data, list):
            for category_item in categories_data:
                if category_item:
                    category_name = ''
                    category_id_tn = None
                    category_handle = ''
                    
                    if isinstance(category_item, dict):
                        # Extraer nombre (puede ser dict multiidioma)
                        name_field = category_item.get('name', '')
                        if name_field:
                            if isinstance(name_field, dict):
                                category_name = self._extract_multilang_value(name_field, '')
                            else:
                                category_name = str(name_field).strip()
                        
                        category_id_tn = category_item.get('id')
                        category_handle = category_item.get('handle', '')
                    else:
                        # Si es un string o número, intentar obtener desde API
                        category_id_str = str(category_item).strip()
                        if category_id_str.isdigit():
                            try:
                                category_response = self._make_request('GET', f'categories/{category_id_str}', retry=False, tn_config=tn_config)
                                if category_response and isinstance(category_response, dict):
                                    category_name_field = category_response.get('name', '')
                                    if category_name_field:
                                        if isinstance(category_name_field, dict):
                                            category_name = self._extract_multilang_value(category_name_field, '')
                                        else:
                                            category_name = str(category_name_field).strip()
                                    category_id_tn = category_response.get('id')
                                    category_handle = category_response.get('handle', '')
                            except Exception as e:
                                _logger.warning("⚠️ No se pudo obtener categoría desde API: %s", e)
                        else:
                            category_name = category_id_str
                    
                    if category_name:
                        # Buscar o crear categoría
                        category_record = self.env['tn.publication.category'].search([
                            ('name', '=', category_name)
                        ], limit=1)
                        
                        if not category_record:
                            category_record = self.env['tn.publication.category'].create({
                                'name': category_name,
                                'tn_category_id': category_id_tn,
                                'handle': category_handle
                            })
                        
                        category_ids.append(category_record.id)
        
        # Si es un string o número único
        elif isinstance(categories_data, (str, int)):
            category_id_str = str(categories_data).strip()
            if category_id_str.isdigit():
                try:
                    category_response = self._make_request('GET', f'categories/{category_id_str}', retry=False, tn_config=tn_config)
                    if category_response and isinstance(category_response, dict):
                        category_name_field = category_response.get('name', '')
                        if category_name_field:
                            if isinstance(category_name_field, dict):
                                category_name = self._extract_multilang_value(category_name_field, '')
                            else:
                                category_name = str(category_name_field).strip()
                            
                            if category_name:
                                category_record = self.env['tn.publication.category'].search([
                                    ('name', '=', category_name)
                                ], limit=1)
                                
                                if not category_record:
                                    category_record = self.env['tn.publication.category'].create({
                                        'name': category_name,
                                        'tn_category_id': category_response.get('id'),
                                        'handle': category_response.get('handle', '')
                                    })
                                
                                category_ids.append(category_record.id)
                except Exception as e:
                    _logger.warning("⚠️ No se pudo obtener categoría desde API: %s", e)
            else:
                # Es un string con nombre de categoría
                category_record = self.env['tn.publication.category'].search([
                    ('name', '=', category_id_str)
                ], limit=1)
                
                if not category_record:
                    category_record = self.env['tn.publication.category'].create({
                        'name': category_id_str
                    })
                
                category_ids.append(category_record.id)
        
        return [(6, 0, category_ids)] if category_ids else [(5, 0, 0)]

    def _create_publication_from_tn_data(self, product_data, tn_config=None, download_images=True):
        """Crea o actualiza una publicación desde datos de TiendaNube
        
        Args:
            product_data: Datos del producto desde TiendaNube
            tn_config: Registro de tn.config a usar para asignar a la publicación
            download_images: Si False, solo guarda URLs (mucho más rápido en import masivo)
        """
        try:
            tn_product_id = product_data.get('id')
            
            # Log para debugging - mostrar estructura de datos
            _logger.info("📦 Importando producto ID: %s. Keys disponibles: %s", 
                         tn_product_id, list(product_data.keys()))
            _logger.debug("📦 Datos completos del producto: %s", str(product_data)[:1000])

            # Siempre obtener el producto completo por ID para tener las variantes reales.
            # GET /products (lista) puede no traer variantes o traer solo una variante virtual.
            if tn_product_id:
                try:
                    full_product = self._make_request('GET', f'/products/{tn_product_id}', retry=False, tn_config=tn_config)
                    if full_product and isinstance(full_product, dict):
                        categories_backup = product_data.get('categories')
                        product_data = dict(full_product)
                        if categories_backup and not product_data.get('categories'):
                            product_data['categories'] = categories_backup
                        _logger.info("📦 Producto %s: GET /products/%s -> %d variantes", tn_product_id, tn_product_id, len(product_data.get('variants') or []))
                except Exception as e:
                    _logger.warning("⚠️ No se pudo obtener producto completo %s: %s", tn_product_id, e)

            variants_from_api = product_data.get('variants') or []
            if not isinstance(variants_from_api, list):
                variants_from_api = []
            # Decisión fija: si la API devuelve 2 o más variantes → una publicación por variante (Nombre producto | Nombre variante)
            import_at_variant_level = len(variants_from_api) >= 2
            _logger.info("📦 Producto %s: variantes en API=%d, import_at_variant_level=%s", tn_product_id, len(variants_from_api), import_at_variant_level)

            option_names = {
                'option1': product_data.get('option1_name', ''),
                'option2': product_data.get('option2_name', ''),
                'option3': product_data.get('option3_name', ''),
            }
            # Lista para el bucle: si import at variant level, construir una entrada por variante desde la API (sin filtrar)
            if import_at_variant_level:
                real_variants_list = self._build_variant_list_from_api(product_data, option_names)
                _logger.info("📦 Producto %s: real_variants_list construida=%d entradas", tn_product_id, len(real_variants_list))
            else:
                real_variants_list = self._get_real_variants_import_list(product_data, option_names)

            # Extraer título del producto (puede venir como dict multiidioma)
            title = self._extract_multilang_value(product_data.get('name'), 'Sin título')
            
            # Extraer handle (puede venir como dict multiidioma)
            handle = self._extract_multilang_value(product_data.get('handle'), '')
            
            # Extraer descripción (puede venir en diferentes campos)
            description = ''
            description_raw = product_data.get('body_html') or product_data.get('body') or product_data.get('description') or product_data.get('description_html', '')
            
            if description_raw:
                if isinstance(description_raw, dict):
                    description = self._extract_multilang_value(description_raw, '')
                elif isinstance(description_raw, str):
                    description = description_raw.strip()
                else:
                    description = str(description_raw).strip()
            
            # Log para debugging
            _logger.info("📝 Descripción extraída: %s caracteres (de body_html=%s, body=%s)", 
                        len(description) if description else 0, 
                        bool(product_data.get('body_html')), 
                        bool(product_data.get('body')))
            
            # Extraer marca (puede venir como vendor, brand, o en metafields)
            marca = ''
            # Intentar diferentes campos posibles
            marca_raw = product_data.get('vendor') or product_data.get('brand') or product_data.get('vendor_name', '')
            
            if marca_raw:
                if isinstance(marca_raw, dict):
                    marca = self._extract_multilang_value(marca_raw, '')
                elif isinstance(marca_raw, str):
                    marca = marca_raw.strip()
                else:
                    marca = str(marca_raw).strip()
            
            # Si no se encontró, intentar en metafields
            if not marca:
                metafields = product_data.get('metafields', [])
                if isinstance(metafields, list):
                    for metafield in metafields:
                        if isinstance(metafield, dict):
                            key = metafield.get('key', '').lower()
                            if 'brand' in key or 'marca' in key:
                                value = metafield.get('value', '')
                                if value:
                                    marca = str(value).strip()
                                    break
            
            # Log para debugging
            _logger.info("🏷️ Marca extraída: '%s' (de vendor=%s, brand=%s)", 
                        marca, product_data.get('vendor'), product_data.get('brand'))
            
            # Extraer categorías (Many2many) - puede venir en diferentes campos
            categories_data = (product_data.get('categories') or 
                             product_data.get('category') or 
                             product_data.get('category_ids', []))
            
            _logger.info("🔍 Buscando categorías. categories_data=%s (tipo: %s)", 
                        categories_data, type(categories_data).__name__)
            
            categoria_ids = self._import_categories(categories_data, tn_config=tn_config)
            
            # Extraer tags correctamente (Many2many)
            tags_raw = product_data.get('tags', [])
            _logger.info("📋 Tags recibidos del producto: %s (tipo: %s)", tags_raw, type(tags_raw).__name__)
            tag_ids = self._import_tags(tags_raw)
            
            # Obtener nombres de opciones del producto (si están disponibles)
            option_names = {
                'option1': product_data.get('option1_name', ''),
                'option2': product_data.get('option2_name', ''),
                'option3': product_data.get('option3_name', ''),
            }
            
            # Extraer SKU y código de barras (pueden venir en el producto o en la primera variante)
            sku = product_data.get('sku', '')
            barcode = product_data.get('barcode', '')
            
            # Si no vienen en el producto, intentar desde la primera variante
            if not sku and product_data.get('variants'):
                first_variant = product_data['variants'][0] if isinstance(product_data['variants'], list) else {}
                sku = first_variant.get('sku', '')
                barcode = first_variant.get('barcode', '') or barcode
            
            # Extraer dimensiones físicas del producto
            # Primero intentar desde el producto, luego desde las variantes
            weight = product_data.get('weight')
            width = product_data.get('width')
            height = product_data.get('height')
            depth = product_data.get('depth')
            
            # Si no hay dimensiones en el producto, buscar en las variantes
            if not weight or not width or not height or not depth:
                variants_data = product_data.get('variants', [])
                if variants_data and isinstance(variants_data, list) and len(variants_data) > 0:
                    # Usar la primera variante que tenga dimensiones
                    for variant in variants_data:
                        if not weight and variant.get('weight'):
                            weight = variant.get('weight')
                        if not width and variant.get('width'):
                            width = variant.get('width')
                        if not height and variant.get('height'):
                            height = variant.get('height')
                        if not depth and variant.get('depth'):
                            depth = variant.get('depth')
                        # Si ya tenemos todas las dimensiones, salir
                        if weight and width and height and depth:
                            break
            
            # Convertir a float si vienen como string
            try:
                weight = float(weight) if weight else 0.0
            except (ValueError, TypeError):
                weight = 0.0
            try:
                width = float(width) if width else 0.0
            except (ValueError, TypeError):
                width = 0.0
            try:
                height = float(height) if height else 0.0
            except (ValueError, TypeError):
                height = 0.0
            try:
                depth = float(depth) if depth else 0.0
            except (ValueError, TypeError):
                depth = 0.0
            
            # Log de dimensiones importadas
            if weight or width or height or depth:
                _logger.info("📏 Dimensiones importadas - weight: %s, width: %s, height: %s, depth: %s", 
                           weight, width, height, depth)
            
            # Preparar datos
            vals = {
                'tn_product_id': tn_product_id,
                'title': title,
                'description': description,
                'description_text': self._html_to_text(description),
                'handle': handle,
                'published': product_data.get('published', False),
                'marca': marca,
                'sku': sku,
                'barcode': barcode,
                'weight': weight,
                'width': width,
                'height': height,
                'depth': depth,
                'categoria_ids': categoria_ids,
                'tag_ids': tag_ids,
                'sync_state': 'synced',
                'last_sync_date': fields.Datetime.now(),
            }
            
            # Asignar cuenta si se proporciona
            if tn_config:
                vals['tn_config_id'] = tn_config.id
            
            # Log final antes de guardar
            _logger.info("💾 Guardando publicación - Categorías: %s, Tags: %s",
                        categoria_ids, tag_ids)
            if tn_config:
                _logger.info("🔑 Asignando cuenta: %s (ID: %d)", tn_config.name, tn_config.id)
            
            if import_at_variant_level:
                # Importación a nivel variante: una publicación por variante con título "Producto | Variante"
                first_publication = None
                for item in real_variants_list:
                    vdata = item['variant_data']
                    vname = item['variant_name']
                    vvals = item['variant_vals']
                    tn_variant_id = vdata.get('id')
                    publication = self.env['tn.publication'].search([
                        ('tn_product_id', '=', tn_product_id),
                        ('main_tn_variant_id', '=', tn_variant_id),
                    ], limit=1)
                    pub_vals = dict(vals)
                    pub_vals['title'] = title + " | " + vname
                    pub_vals['main_tn_variant_id'] = tn_variant_id
                    if publication:
                        publication.write(pub_vals)
                        _logger.info("✅ Publicación por variante actualizada: ID=%s (%s | %s)", publication.id, title, vname)
                    else:
                        publication = self.env['tn.publication'].create(pub_vals)
                        _logger.info("✅ Publicación por variante creada: ID=%s (%s | %s)", publication.id, title, vname)
                    publication.variant_ids.unlink()
                    vvals['publication_id'] = publication.id
                    self.env['tn.publication.variant'].create(vvals)
                    self._import_images(publication, product_data.get('images', []), download_binary=download_images)
                    try:
                        self._import_custom_fields(publication, tn_product_id)
                    except Exception as e:
                        _logger.warning("⚠️ No se pudieron importar campos personalizados para el producto %s: %s", tn_product_id, e)
                    # Enlace por SKU (default_code) igual que Auto-relacionar por SKU
                    self._link_publication_variants_to_odoo_by_sku(publication)
                    # Si no hubo match por SKU, intentar por nombre/barcode de la primera variante del producto
                    if not publication.odoo_product_id:
                        self._sync_to_odoo_product(publication, product_data)
                    if first_publication is None:
                        first_publication = publication
                return first_publication
            else:
                # Importación a nivel producto (sin variantes o una sola variante): una publicación por producto
                publication = self.env['tn.publication'].search([
                    ('tn_product_id', '=', tn_product_id),
                    ('main_tn_variant_id', 'in', [False, 0]),
                ], limit=1)
                vals['main_tn_variant_id'] = 0
                if publication:
                    publication.write(vals)
                    _logger.info("✅ Publicación actualizada: ID=%s", publication.id)
                else:
                    publication = self.env['tn.publication'].create(vals)
                    _logger.info("✅ Publicación creada: ID=%s", publication.id)
                self._import_images(publication, product_data.get('images', []), download_binary=download_images)
                self._import_variants(publication, product_data.get('variants', []), option_names=option_names, product_data=product_data)
                try:
                    self._import_custom_fields(publication, tn_product_id)
                except Exception as e:
                    _logger.warning("⚠️ No se pudieron importar campos personalizados para el producto %s: %s", tn_product_id, e)
                self._sync_to_odoo_product(publication, product_data)
                # Enlace por SKU por variante (default_code), igual que Auto-relacionar por SKU
                self._link_publication_variants_to_odoo_by_sku(publication)
                return publication
        except Exception as e:
            _logger.exception("Error al crear publicación desde datos TN: %s", e)
            return None

    def _import_images(self, publication, images_data, download_binary=True):
        """Importa imágenes desde TiendaNube (binario opcional)."""
        publication.image_ids.unlink()

        for idx, image_data in enumerate(images_data, start=1):
            image_url = image_data.get('src') or image_data.get('url', '')
            if not image_url:
                continue
            vals = {
                'publication_id': publication.id,
                'name': image_data.get('alt', f'Imagen {idx}'),
                'image_url': image_url,
                'position': image_data.get('position', idx),
                'tn_image_id': image_data.get('id'),
            }
            if download_binary:
                try:
                    response = requests.get(image_url, timeout=15)
                    if response.status_code == 200:
                        vals['image'] = base64.b64encode(response.content).decode('utf-8')
                except Exception as e:
                    _logger.warning("Error al descargar imagen %s: %s", image_url, e)
            self.env['tn.publication.image'].create(vals)

    def _build_one_variant_import_data(self, variant_data, product_data, option_names):
        """
        Construye para una variante su nombre y los vals para crear tn.publication.variant.
        Retorna (variant_name, variant_vals_dict) o None si la variante no tiene valores de atributos.
        variant_vals_dict no incluye publication_id.
        """
        if option_names is None:
            option_names = {}
        if product_data is None:
            product_data = {}
        values_list = variant_data.get('values', [])
        option1_value = option2_value = option3_value = ''
        if values_list and isinstance(values_list, list):
            for idx, value_item in enumerate(values_list):
                if idx == 0:
                    option1_value = self._extract_multilang_value(value_item, '') if isinstance(value_item, dict) else str(value_item).strip()
                elif idx == 1:
                    option2_value = self._extract_multilang_value(value_item, '') if isinstance(value_item, dict) else str(value_item).strip()
                elif idx == 2:
                    option3_value = self._extract_multilang_value(value_item, '') if isinstance(value_item, dict) else str(value_item).strip()
        else:
            option1_value = (variant_data.get('option1') or '').strip()
            option2_value = (variant_data.get('option2') or '').strip()
            option3_value = (variant_data.get('option3') or '').strip()
        # Importar todas las variantes aunque no tengan opciones; luego se matchean por SKU
        attributes = product_data.get('attributes', [])
        option1_name = option2_name = option3_name = ''
        if attributes and isinstance(attributes, list):
            for idx, attr in enumerate(attributes):
                if idx == 0:
                    option1_name = self._extract_multilang_value(attr, 'Opción 1') if isinstance(attr, dict) else str(attr).strip()
                elif idx == 1:
                    option2_name = self._extract_multilang_value(attr, 'Opción 2') if isinstance(attr, dict) else str(attr).strip()
                elif idx == 2:
                    option3_name = self._extract_multilang_value(attr, 'Opción 3') if isinstance(attr, dict) else str(attr).strip()
        if not option1_name:
            option1_name = option_names.get('option1', 'Opción 1')
        if not option2_name:
            option2_name = option_names.get('option2', 'Opción 2')
        if not option3_name:
            option3_name = option_names.get('option3', 'Opción 3')
        variant_name_parts = []
        if option1_name and option1_value:
            variant_name_parts.append(f"{option1_name}: {option1_value}")
        if option2_name and option2_value:
            variant_name_parts.append(f"{option2_name}: {option2_value}")
        if option3_name and option3_value:
            variant_name_parts.append(f"{option3_name}: {option3_value}")
        variant_name = ' / '.join(variant_name_parts) if variant_name_parts else (
            variant_data.get('title') or variant_data.get('name') or variant_data.get('title_html') or 'Variante'
        )
        if isinstance(variant_name, dict):
            variant_name = self._extract_multilang_value(variant_name, 'Variante')
        variant_name = (variant_name or 'Variante').strip() if isinstance(variant_name, str) else 'Variante'
        stock_value = variant_data.get('stock') or variant_data.get('inventory_quantity') or 0
        try:
            stock_float = float(stock_value) if stock_value is not None else 0.0
        except (ValueError, TypeError):
            stock_float = 0.0
        try:
            price_float = float(variant_data.get('price') or 0)
        except (ValueError, TypeError):
            price_float = 0.0
        try:
            weight_float = float(variant_data.get('weight') or 0)
        except (ValueError, TypeError):
            weight_float = 0.0
        try:
            width_float = float(variant_data.get('width') or 0)
        except (ValueError, TypeError):
            width_float = 0.0
        try:
            height_float = float(variant_data.get('height') or 0)
        except (ValueError, TypeError):
            height_float = 0.0
        try:
            depth_float = float(variant_data.get('depth') or 0)
        except (ValueError, TypeError):
            depth_float = 0.0
        barcode = (variant_data.get('barcode') or '').strip()
        variant_vals = {
            'name': variant_name,
            'sku': variant_data.get('sku', ''),
            'price': price_float,
            'stock': stock_float,
            'option1_name': option1_name if option1_value else '',
            'option1_value': option1_value,
            'option2_name': option2_name if option2_value else '',
            'option2_value': option2_value,
            'option3_name': option3_name if option3_value else '',
            'option3_value': option3_value,
            'weight': weight_float,
            'width': width_float,
            'height': height_float,
            'depth': depth_float,
            'barcode': barcode,
            'tn_variant_id': variant_data.get('id'),
        }
        return (variant_name, variant_vals)

    def _build_variant_list_from_api(self, product_data, option_names=None):
        """
        Construye una lista con una entrada por cada variante en product_data['variants'].
        Usado para import a nivel variante: título "Producto | Variante", cada una es una publicación.
        No filtra; garantiza len(result) == len(variants).
        """
        if option_names is None:
            option_names = {}
        variants_data = product_data.get('variants', []) or []
        if not isinstance(variants_data, list):
            return []
        result = []
        attributes = product_data.get('attributes', []) or []
        for idx, variant_data in enumerate(variants_data):
            # Nombre de variante: values (multilang), option1/2/3, o "Variante N"
            variant_name = 'Variante %d' % (idx + 1)
            values_list = variant_data.get('values', [])
            if values_list and isinstance(values_list, list):
                parts = []
                for v in values_list[:3]:
                    if isinstance(v, dict):
                        parts.append(self._extract_multilang_value(v, ''))
                    else:
                        parts.append(str(v).strip())
                if parts:
                    variant_name = ' / '.join(p for p in parts if p)
            else:
                o1 = (variant_data.get('option1') or '').strip()
                o2 = (variant_data.get('option2') or '').strip()
                o3 = (variant_data.get('option3') or '').strip()
                if o1 or o2 or o3:
                    variant_name = ' / '.join(x for x in [o1, o2, o3] if x)
            try:
                price_float = float(variant_data.get('price') or 0)
            except (ValueError, TypeError):
                price_float = 0.0
            try:
                stock_float = float(variant_data.get('stock') or variant_data.get('inventory_quantity') or 0)
            except (ValueError, TypeError):
                stock_float = 0.0
            option1_name = option_names.get('option1', '') or (attributes[0] if attributes and len(attributes) > 0 else '')
            if isinstance(option1_name, dict):
                option1_name = self._extract_multilang_value(option1_name, 'Opción 1')
            option2_name = option_names.get('option2', '') or (attributes[1] if attributes and len(attributes) > 1 else '')
            if isinstance(option2_name, dict):
                option2_name = self._extract_multilang_value(option2_name, 'Opción 2')
            option3_name = option_names.get('option3', '') or (attributes[2] if attributes and len(attributes) > 2 else '')
            if isinstance(option3_name, dict):
                option3_name = self._extract_multilang_value(option3_name, 'Opción 3')
            o1v = (variant_data.get('option1') or '').strip()
            o2v = (variant_data.get('option2') or '').strip()
            o3v = (variant_data.get('option3') or '').strip()
            if values_list and isinstance(values_list, list):
                for i, v in enumerate(values_list[:3]):
                    val = self._extract_multilang_value(v, '') if isinstance(v, dict) else str(v).strip()
                    if i == 0:
                        o1v = val
                    elif i == 1:
                        o2v = val
                    elif i == 2:
                        o3v = val
            barcode = (variant_data.get('barcode') or '').strip()
            try:
                w_float = float(variant_data.get('weight') or 0)
            except (ValueError, TypeError):
                w_float = 0.0
            try:
                width_float = float(variant_data.get('width') or 0)
            except (ValueError, TypeError):
                width_float = 0.0
            try:
                height_float = float(variant_data.get('height') or 0)
            except (ValueError, TypeError):
                height_float = 0.0
            try:
                depth_float = float(variant_data.get('depth') or 0)
            except (ValueError, TypeError):
                depth_float = 0.0
            variant_vals = {
                'name': variant_name,
                'sku': variant_data.get('sku', ''),
                'price': price_float,
                'stock': stock_float,
                'option1_name': option1_name or '', 'option1_value': o1v,
                'option2_name': option2_name or '', 'option2_value': o2v,
                'option3_name': option3_name or '', 'option3_value': o3v,
                'weight': w_float, 'width': width_float, 'height': height_float, 'depth': depth_float,
                'barcode': barcode,
                'tn_variant_id': variant_data.get('id'),
            }
            result.append({
                'variant_data': variant_data,
                'variant_name': variant_name,
                'variant_vals': variant_vals,
            })
        return result

    def _get_real_variants_import_list(self, product_data, option_names=None):
        """
        Retorna lista de variantes para importación (una por variante en TN).
        Cada elemento: {'variant_data': dict, 'variant_name': str, 'variant_vals': dict (sin publication_id)}.
        Incluye todas las variantes; si falla el parsing de una, se añade con datos mínimos.
        """
        if option_names is None:
            option_names = {}
        variants_data = product_data.get('variants', []) or []
        if not variants_data or not isinstance(variants_data, list):
            return []
        result = []
        for variant_data in variants_data:
            try:
                one = self._build_one_variant_import_data(variant_data, product_data, option_names)
            except Exception as e:
                _logger.warning("⚠️ Fallo parsing variante (id=%s), usando datos mínimos: %s", variant_data.get('id'), e)
                one = None
            if one:
                variant_name, variant_vals = one
                result.append({
                    'variant_data': variant_data,
                    'variant_name': variant_name,
                    'variant_vals': variant_vals,
                })
            else:
                # Fallback: una entrada por variante con datos crudos
                v_id = variant_data.get('id')
                name = 'Variante'
                if isinstance(variant_data.get('values'), list) and variant_data['values']:
                    name = ' / '.join(
                        self._extract_multilang_value(v, str(v)) if isinstance(v, dict) else str(v)
                        for v in variant_data['values'][:3]
                    )
                price = float(variant_data.get('price') or 0)
                stock = float(variant_data.get('stock') or variant_data.get('inventory_quantity') or 0)
                variant_vals = {
                    'name': name or 'Variante',
                    'sku': variant_data.get('sku', ''),
                    'price': price,
                    'stock': stock,
                    'option1_name': '', 'option1_value': '', 'option2_name': '', 'option2_value': '',
                    'option3_name': '', 'option3_value': '',
                    'weight': 0, 'width': 0, 'height': 0, 'depth': 0, 'barcode': variant_data.get('barcode', '') or '',
                    'tn_variant_id': v_id,
                }
                result.append({
                    'variant_data': variant_data,
                    'variant_name': variant_vals['name'],
                    'variant_vals': variant_vals,
                })
        return result

    def _import_variants(self, publication, variants_data, option_names=None, product_data=None):
        """Importa variantes desde TiendaNube. Solo crea registros si hay variantes reales con valores de atributos."""
        if option_names is None:
            option_names = {}
        if product_data is None:
            product_data = {}
        
        # Si no hay variantes o la lista está vacía, eliminar existentes y salir
        if not variants_data or not isinstance(variants_data, list) or len(variants_data) == 0:
            _logger.info("📦 No hay variantes para importar, eliminando variantes existentes")
            publication.variant_ids.unlink()
            return
        
        # Verificar si hay variantes REALES (con valores de atributos)
        # En TiendaNube, un producto sin variantes puede venir con una lista con una sola variante sin valores
        has_real_variants = False
        for variant_data in variants_data:
            # Verificar si esta variante tiene valores de atributos
            values_list = variant_data.get('values', [])
            if values_list and isinstance(values_list, list) and len(values_list) > 0:
                # Tiene valores en formato 'values'
                has_real_variants = True
                break
            else:
                # Verificar option1, option2, option3
                option1 = variant_data.get('option1', '')
                option2 = variant_data.get('option2', '')
                option3 = variant_data.get('option3', '')
                if (option1 and str(option1).strip()) or (option2 and str(option2).strip()) or (option3 and str(option3).strip()):
                    has_real_variants = True
                    break
        
        # Si no hay variantes reales (producto sin variantes), eliminar existentes y salir
        if not has_real_variants:
            _logger.info("📦 Producto sin variantes reales (solo producto base), eliminando variantes existentes")
            publication.variant_ids.unlink()
            return
        
        # Eliminar variantes existentes
        publication.variant_ids.unlink()
        
        # Procesar solo variantes que tengan valores de atributos
        for variant_data in variants_data:
            _logger.info("📦 Procesando variante: %s", variant_data)
            
            # Extraer opciones - pueden venir como 'values' (lista) o 'option1', 'option2', 'option3'
            values_list = variant_data.get('values', [])
            option1_value = ''
            option2_value = ''
            option3_value = ''
            
            # Si viene como 'values' (lista de dicts multiidioma)
            if values_list and isinstance(values_list, list):
                for idx, value_item in enumerate(values_list):
                    if idx == 0:
                        option1_value = self._extract_multilang_value(value_item, '') if isinstance(value_item, dict) else str(value_item).strip()
                    elif idx == 1:
                        option2_value = self._extract_multilang_value(value_item, '') if isinstance(value_item, dict) else str(value_item).strip()
                    elif idx == 2:
                        option3_value = self._extract_multilang_value(value_item, '') if isinstance(value_item, dict) else str(value_item).strip()
            else:
                # Si viene como option1, option2, option3
                option1_value = variant_data.get('option1', '') or ''
                option2_value = variant_data.get('option2', '') or ''
                option3_value = variant_data.get('option3', '') or ''
            
            # Normalizar valores (trim)
            option1_value = str(option1_value).strip() if option1_value else ''
            option2_value = str(option2_value).strip() if option2_value else ''
            option3_value = str(option3_value).strip() if option3_value else ''
            
            # Si esta variante no tiene valores de atributos, saltarla (es producto base sin variantes)
            if not option1_value and not option2_value and not option3_value:
                _logger.info("⚠️ Saltando variante sin valores de atributos (producto base): %s", variant_data.get('id'))
                continue
            
            # Los nombres de las opciones pueden venir en variant_data, en el producto (attributes), o en option_names
            # Buscar en attributes del producto
            attributes = product_data.get('attributes', [])
            option1_name = ''
            option2_name = ''
            option3_name = ''
            
            if attributes and isinstance(attributes, list):
                for idx, attr in enumerate(attributes):
                    if idx == 0:
                        option1_name = self._extract_multilang_value(attr, 'Opción 1') if isinstance(attr, dict) else str(attr).strip()
                    elif idx == 1:
                        option2_name = self._extract_multilang_value(attr, 'Opción 2') if isinstance(attr, dict) else str(attr).strip()
                    elif idx == 2:
                        option3_name = self._extract_multilang_value(attr, 'Opción 3') if isinstance(attr, dict) else str(attr).strip()
            
            # Si no se encontraron en attributes, usar option_names del producto
            if not option1_name:
                option1_name = option_names.get('option1', 'Opción 1')
            if not option2_name:
                option2_name = option_names.get('option2', 'Opción 2')
            if not option3_name:
                option3_name = option_names.get('option3', 'Opción 3')
            
            # Construir nombre de la variante combinando las opciones
            variant_name_parts = []
            if option1_name and option1_value:
                variant_name_parts.append(f"{option1_name}: {option1_value}")
            if option2_name and option2_value:
                variant_name_parts.append(f"{option2_name}: {option2_value}")
            if option3_name and option3_value:
                variant_name_parts.append(f"{option3_name}: {option3_value}")
            
            # Si no hay opciones, usar el título o nombre de la variante, o un nombre por defecto
            variant_name = ' / '.join(variant_name_parts) if variant_name_parts else (
                variant_data.get('title', '') or 
                variant_data.get('name', '') or 
                variant_data.get('title_html', '') or
                'Variante'
            )
            
            # Si el nombre viene como dict multiidioma, extraerlo
            if isinstance(variant_name, dict):
                variant_name = self._extract_multilang_value(variant_name, 'Variante')
            
            # Asegurar que variant_name sea string
            if not isinstance(variant_name, str):
                variant_name = str(variant_name) if variant_name else 'Variante'
            
            # Limpiar el nombre
            variant_name = variant_name.strip()
            if not variant_name:
                variant_name = 'Variante'
            
            _logger.info("📦 Creando variante - Nombre: '%s', SKU: %s, Opciones: %s/%s, %s/%s, %s/%s",
                        variant_name, variant_data.get('sku', ''),
                        option1_name, option1_value, option2_name, option2_value, option3_name, option3_value)
            
            # Manejar stock - puede venir en diferentes campos y puede ser None
            stock_value = variant_data.get('stock') or variant_data.get('inventory_quantity') or 0
            if stock_value is None:
                stock_value = 0
            try:
                stock_float = float(stock_value) if stock_value else 0.0
            except (ValueError, TypeError):
                stock_float = 0.0
            
            # Manejar precio
            price_value = variant_data.get('price', 0)
            try:
                price_float = float(price_value) if price_value else 0.0
            except (ValueError, TypeError):
                price_float = 0.0
            
            # Manejar peso
            weight_value = variant_data.get('weight', 0)
            try:
                weight_float = float(weight_value) if weight_value else 0.0
            except (ValueError, TypeError):
                weight_float = 0.0
            
            # Manejar dimensiones (width, height, depth)
            width_value = variant_data.get('width', 0)
            try:
                width_float = float(width_value) if width_value else 0.0
            except (ValueError, TypeError):
                width_float = 0.0
            
            height_value = variant_data.get('height', 0)
            try:
                height_float = float(height_value) if height_value else 0.0
            except (ValueError, TypeError):
                height_float = 0.0
            
            depth_value = variant_data.get('depth', 0)
            try:
                depth_float = float(depth_value) if depth_value else 0.0
            except (ValueError, TypeError):
                depth_float = 0.0
            
            # Manejar código de barras
            barcode = variant_data.get('barcode', '') or ''
            
            variant_record = self.env['tn.publication.variant'].create({
                'publication_id': publication.id,
                'name': variant_name,
                'sku': variant_data.get('sku', ''),
                'price': price_float,
                'stock': stock_float,
                'option1_name': option1_name if option1_value else '',
                'option1_value': option1_value,
                'option2_name': option2_name if option2_value else '',
                'option2_value': option2_value,
                'option3_name': option3_name if option3_value else '',
                'option3_value': option3_value,
                'weight': weight_float,
                'width': width_float,
                'height': height_float,
                'depth': depth_float,
                'barcode': barcode,
                'tn_variant_id': variant_data.get('id'),
            })
            
            _logger.info("✅ Variante creada - ID: %s, Nombre guardado: '%s'", variant_record.id, variant_record.name)

    def _sync_to_odoo_product(self, publication, product_data):
        """Busca y actualiza un producto en Odoo desde datos de TiendaNube. NO crea productos nuevos.
        Matcheo solo por default_code (Referencia interna). No modifica list_price del producto."""
        try:
            # Buscar producto existente por SKU (default_code), barcode o nombre
            product_template = None
            sku = None
            if product_data.get('variants'):
                first_variant = product_data['variants'][0]
                sku = (first_variant.get('sku') or '').strip()
            if not sku and publication.variant_ids:
                sku = (publication.variant_ids[0].sku or '').strip()
            if not sku and publication.sku:
                sku = (publication.sku or '').strip()
            if sku:
                # Solo matcheo por Referencia interna (default_code)
                product_template = self.env['product.template'].search([
                    ('default_code', '=', sku),
                    ('active', '=', True)
                ], limit=1)
                if not product_template:
                    odoo_variant = self.env['product.product'].search([
                        ('default_code', '=', sku),
                        ('active', '=', True)
                    ], limit=1)
                    if odoo_variant:
                        product_template = odoo_variant.product_tmpl_id
            
            # Solo actualizar si existe, NO crear productos nuevos (sin tocar list_price de Odoo)
            if product_template:
                product_vals = {
                    'name': publication.title,
                    'description': publication.description or '',
                    'description_sale': publication.description_text or '',
                    'sale_ok': True,
                    'purchase_ok': False,
                }
                product_template.write(product_vals)
                
                # Actualizar relación
                publication.odoo_product_id = product_template
                _logger.info("✅ Producto de Odoo actualizado: ID=%s, Nombre=%s", product_template.id, product_template.name)
            else:
                # No crear producto nuevo, solo log
                _logger.info("⚠️ No se encontró producto de Odoo relacionado (SKU o nombre). No se creará producto nuevo.")
                # Limpiar relación si no existe producto
                publication.odoo_product_id = False
            
        except Exception as e:
            _logger.warning("Error al sincronizar producto a Odoo: %s", e)

    def _link_publication_variants_to_odoo_by_sku(self, publication):
        """Para cada variante de la publicación con SKU, busca product.product por default_code y asigna odoo_variant_id.
        Misma lógica que action_auto_link_products_by_sku, para que el import matchee igual que el botón."""
        if not publication.variant_ids:
            return
        for variant in publication.variant_ids:
            sku = (variant.sku or '').strip()
            if not sku or variant.odoo_variant_id:
                continue
            odoo_variant = self.env['product.product'].search([
                ('default_code', '=', sku),
                ('active', '=', True)
            ], limit=1)
            if not odoo_variant:
                continue
            variant.odoo_variant_id = odoo_variant.id
            if not publication.odoo_product_id:
                publication.with_context(tn_sku_link_only=True).write(
                    {'odoo_product_id': odoo_variant.product_tmpl_id.id}
                )
            _logger.info("✅ Enlace por SKU en import: variante '%s' -> %s (default_code=%s)",
                         variant.name or 'sin nombre', odoo_variant.display_name, sku)

    def sync_published_status_to_tn(self, publication):
        """Actualiza solo el flag published en TiendaNube (pausa/despausa automática)."""
        if not publication.tn_product_id:
            return False
        tn_config = publication.tn_config_id if publication.tn_config_id else None
        config = self._get_config(tn_config=tn_config)
        if not config.get('access_token'):
            raise UserError(_('Debe configurar el Access Token en la cuenta de TiendaNube'))
        payload = {'published': bool(publication.published)}
        endpoint = f'/products/{publication.tn_product_id}'
        _logger.info(
            "📤 Sincronizando solo published=%s en TN (product_id=%s)",
            payload['published'],
            publication.tn_product_id,
        )
        self._make_request('PUT', endpoint, data=payload, tn_config=tn_config)
        return True

    def export_publication(self, publication):
        """Exporta una publicación a TiendaNube (crear o actualizar)"""
        export_start = time.time()
        tn_config = publication.tn_config_id if publication.tn_config_id else None
        try:
            config = self._get_config(tn_config=tn_config)
            store_id = config.get('store_id', '').strip()
            
            if not store_id:
                raise UserError(_('Debe configurar el Store ID en la cuenta de TiendaNube'))
            
            if not config.get('access_token'):
                raise UserError(_('Debe configurar el Access Token en la cuenta de TiendaNube'))

            # Limpiar y normalizar campos
            clean_description = self._clean_html_completely(str(publication.description or ''))
            handle = publication.handle or self._generate_handle(publication.title or '')

            def _ml(value):
                """Convierte a dict multilenguaje esperado por TiendaNube."""
                return {'es': value or ''} if value is not None else {'es': ''}

            # Construir payload respetando formato TiendaNube
            # Según documentación: description es multilenguaje, NO body_html
            product_data = {
                "name": _ml(publication.title or ''),
                "description": _ml(clean_description),  # description multilenguaje según docs
                "handle": handle,
                "published": bool(publication.published),
            }
            
            # Log de descripción
            if clean_description:
                _logger.info("📝 Descripción a enviar: %d caracteres", len(clean_description))
            else:
                _logger.info("📝 Descripción vacía")
            
            # Verificar valores de dimensiones y SKU/barcode en la publicación
            _logger.info("🔍 VALORES EN PUBLICACIÓN:")
            _logger.info("   - weight: %s (tipo: %s)", publication.weight, type(publication.weight).__name__)
            _logger.info("   - width: %s (tipo: %s)", publication.width, type(publication.width).__name__)
            _logger.info("   - height: %s (tipo: %s)", publication.height, type(publication.height).__name__)
            _logger.info("   - depth: %s (tipo: %s)", publication.depth, type(publication.depth).__name__)
            _logger.info("   - sku: '%s' (tipo: %s)", publication.sku, type(publication.sku).__name__)
            _logger.info("   - barcode: '%s' (tipo: %s)", publication.barcode, type(publication.barcode).__name__)
            _logger.info("   - variant_ids count: %d", len(publication.variant_ids))
            
            # NOTA IMPORTANTE según documentación TiendaNube:
            # Si un producto NO tiene variantes definidas, tiene una "variante virtual".
            # Los cambios de precio, stock, dimensiones, SKU, barcode deben hacerse en esa variante virtual,
            # NO en el producto mismo. Por lo tanto, NO agregamos estos campos al producto aquí.
            # Se manejarán en _update_variants_individually después de obtener la variante virtual.
            has_variants = bool(publication.variant_ids)
            _logger.info("🔍 ¿Hay variantes definidas en Odoo? %s (%d)", has_variants, len(publication.variant_ids))
            
            if not has_variants:
                _logger.info("📦 No hay variantes definidas - se actualizará la variante virtual después del PUT del producto")
            else:
                _logger.info("📦 Hay %d variantes, dimensiones y SKU/barcode se manejarán a nivel de VARIANTE", len(publication.variant_ids))
            
            # NOTA: weight, width, height, depth, sku, barcode SIEMPRE se manejan a nivel de VARIANTE
            # (ya sea variante real o variante virtual)
            
            # Marca (brand) - enviar si tiene valor (TiendaNube usa "brand", no "vendor")
            marca_value = (publication.marca or '').strip()
            if marca_value:
                product_data["brand"] = marca_value
                _logger.info("🏷️ Marca a enviar: '%s'", marca_value)
            else:
                _logger.info("🏷️ No hay marca para enviar (publication.marca está vacío)")
            
            # Categorías - array de IDs (enteros) según documentación TiendaNube
            # IMPORTANTE: La API requiere que categories sea una lista de enteros, NO un string
            # Buscar/crear categorías automáticamente si no tienen ID
            category_ids = []
            for cat in publication.categoria_ids:
                if cat.tn_category_id:
                    # Si tiene ID de TiendaNube, usarlo directamente
                    category_ids.append(cat.tn_category_id)
                elif cat.name:
                    # Si no tiene ID, buscar por nombre en TiendaNube
                    try:
                        cat_id = self._find_or_create_category_by_name(cat.name.strip())
                        if cat_id:
                            category_ids.append(cat_id)
                            # Guardar el ID para futuras referencias
                            cat.tn_category_id = cat_id
                            _logger.info("✅ Categoría '%s' encontrada/creada en TiendaNube con ID: %s", cat.name, cat_id)
                        else:
                            _logger.warning("⚠️ Categoría '%s' no encontrada ni creada en TiendaNube, omitiendo", cat.name)
                    except Exception as e:
                        _logger.warning("⚠️ Error buscando/creando categoría '%s' en TiendaNube: %s", cat.name, str(e))
            
            if category_ids:
                product_data["categories"] = category_ids
                _logger.info("📂 Enviando categorías (IDs): %s", category_ids)
            else:
                _logger.info("📂 No se envían categorías (se mantienen las existentes en TiendaNube)")
            
            # Tags - string separado por comas (según documentación TiendaNube)
            # NO enviar tags si es string vacío (regla API TiendaNube)
            if publication.tag_ids:
                tag_names = [tag.name for tag in publication.tag_ids if tag.name]
                if tag_names:
                    product_data["tags"] = ", ".join(tag_names)
                    _logger.info("🏷️ Tags a enviar: '%s'", product_data["tags"])
                else:
                    _logger.info("🗑️ No se enviará campo 'tags': lista de tags vacía")
            else:
                _logger.info("🗑️ No se enviará campo 'tags': no hay tags definidos")

            # Variantes (solo para creación; PUT no acepta variants)
            _logger.info("🔍 Preparando variantes...")
            variants_payload = self._prepare_variants(publication)
            _logger.info("🔍 Variantes preparadas: %d variantes (con valores de atributos)", len(variants_payload))
            
            # Determinar si hay variantes REALES (con valores de atributos)
            # Solo considerar que hay variantes si el payload tiene variantes con values
            has_variants = bool(variants_payload and len(variants_payload) > 0)
            
            # Verificar que todas las variantes tengan values
            if variants_payload:
                variants_without_values = [v for v in variants_payload if not v.get('values')]
                if variants_without_values:
                    _logger.warning("⚠️ Advertencia: Se encontraron %d variantes sin valores de atributos (serán omitidas)", 
                                  len(variants_without_values))
            
            # Preparar attributes - SOLO cuando hay variantes REALES (con values)
            # REGLA CRÍTICA: Si hay variants → SIEMPRE debe haber attributes
            # TiendaNube NO infiere attributes, omitirlos causa error "Variant values should not be repeated"
            # IMPORTANTE: Solo preparar attributes si realmente hay variantes con valores (has_variants)
            attributes_list = self._prepare_attributes(publication) if has_variants else []
            
            if has_variants:
                # REGLA OBLIGATORIA: Si hay variants, SIEMPRE incluir attributes
                if not attributes_list:
                    _logger.error("❌ ERROR CRÍTICO: Hay variantes (%d) pero no se pudieron preparar attributes", len(variants_payload))
                    _logger.error("   Variantes en payload: %s", [v.get('values', 'SIN VALUES') for v in variants_payload])
                    _logger.error("   Variantes en publicación: %d", len(publication.variant_ids))
                    for idx, variant in enumerate(publication.variant_ids, 1):
                        _logger.error("      %d. %s - Opciones: %s/%s, %s/%s, %s/%s", 
                                    idx, variant.name or 'sin nombre',
                                    variant.option1_name or '', variant.option1_value or '',
                                    variant.option2_name or '', variant.option2_value or '',
                                    variant.option3_name or '', variant.option3_value or '')
                    raise UserError(_("Error: No se pudieron preparar los atributos para las variantes. Esto es obligatorio cuando hay variantes. Verifique que las variantes tengan valores de atributos (opciones) definidos."))
                
                product_data["attributes"] = attributes_list
                _logger.info("✅ Producto CON variantes (%d variantes) - Se enviará campo 'attributes' OBLIGATORIAMENTE", 
                           len(variants_payload))
                _logger.info("✅ Attributes preparados: %s", attributes_list)
                
                _logger.debug("🔍 Primera variante: %s", variants_payload[0])
                # Log detallado de cada variante
                for idx, variant_payload in enumerate(variants_payload):
                    _logger.info("📋 VARIANTE #%d:", idx + 1)
                    _logger.info("   - Precio: %s (tipo: %s)", variant_payload.get('price'), type(variant_payload.get('price')).__name__)
                    _logger.info("   - Stock: %s (tipo: %s)", variant_payload.get('stock'), type(variant_payload.get('stock')).__name__)
                    _logger.info("   - Stock Management: %s", variant_payload.get('stock_management'))
                    _logger.info("   - SKU: %s", variant_payload.get('sku', 'NO PRESENTE'))
                    _logger.info("   - Values: %s", variant_payload.get('values', 'NO PRESENTE'))
                    _logger.info("   - ID: %s", variant_payload.get('id', 'NO PRESENTE (nueva variante)'))
                    _logger.info("   - Payload completo: %s", variant_payload)
            else:
                _logger.info("✅ Producto SIN variantes - Se enviará campo 'attributes' si está disponible")
                # Cuando NO hay variantes, attributes es opcional
                if attributes_list:
                    product_data["attributes"] = attributes_list
                    _logger.info("✅ Attributes a enviar (sin variantes): %s", attributes_list)
                else:
                    _logger.info("ℹ️ No hay attributes definidos (producto simple)")
            
            # Construir payload - REGLA: Si hay variants → SIEMPRE incluir attributes
            product_data_post = dict(product_data)
            
            if has_variants:
                # Incluir variants y attributes siempre (POST y PUT)
                product_data_post["variants"] = variants_payload
                
                # Asegurar que attributes esté presente (OBLIGATORIO cuando hay variants)
                if "attributes" not in product_data_post or not product_data_post.get("attributes"):
                    if attributes_list:
                        product_data_post["attributes"] = attributes_list
                        _logger.info("✅ Attributes agregados al payload (OBLIGATORIO con variants)")
                    else:
                        _logger.error("❌ ERROR CRÍTICO: No hay attributes para agregar pero hay variants")
                        raise UserError(_("Error: No se pudieron preparar los atributos. Esto es obligatorio cuando hay variantes."))
                
                _logger.info("✅ Payload: variants=%d, attributes=%d atributos", 
                           len(variants_payload), len(product_data_post.get("attributes", [])))
            else:
                # Si NO hay variantes: NO incluir variants vacío, solo attributes si existen
                if attributes_list:
                    _logger.info("✅ Payload: sin variantes, con attributes (%d atributos)", len(attributes_list))
                else:
                    _logger.info("✅ Payload: producto simple (sin variantes ni attributes)")
            
            # Log del product_data ANTES de strip_empty
            _logger.info("🔍 product_data ANTES de _strip_empty: %s", product_data)
            _logger.info("🔍 product_data_post ANTES de _strip_empty: %s", product_data_post)

            # Remover claves vacías para evitar rechazo por campos inválidos
            # Eliminar campos multilanguage vacíos (description.es == "", name.es == "", etc.)
            def _strip_empty(data: dict):
                """
                Elimina campos vacíos profundamente:
                - Strings vacíos
                - Dicts multilanguage sin valores (ej: {'es': ''})
                - Listas vacías
                - Valores None
                """
                def _is_multilang_empty(value):
                    """Verifica si un dict multilanguage está vacío (todos los valores son strings vacíos)"""
                    if not isinstance(value, dict):
                        return False
                    # Verificar si todos los valores son strings vacíos o None
                    for lang, lang_value in value.items():
                        if lang_value and str(lang_value).strip():
                            return False
                    return True
                
                def _is_list_empty(value):
                    """Verifica si una lista está vacía o contiene solo elementos vacíos"""
                    if not isinstance(value, list):
                        return False
                    if len(value) == 0:
                        return True
                    # Verificar si todos los elementos son vacíos
                    for item in value:
                        if isinstance(item, dict):
                            # Si es un dict multilanguage, verificar si está vacío
                            if not _is_multilang_empty(item):
                                return False
                        elif isinstance(item, str):
                            if item.strip():
                                return False
                        elif item not in (None, '', []):
                            return False
                    return True
                
                def _clean_value(value):
                    """Limpia recursivamente un valor"""
                    if value is None:
                        return None
                    if isinstance(value, str):
                        return value if value.strip() else None
                    if isinstance(value, dict):
                        # Si es multilanguage, verificar si está vacío
                        if _is_multilang_empty(value):
                            return None
                        # Limpiar recursivamente los valores del dict
                        cleaned = {}
                        for k, v in value.items():
                            cleaned_v = _clean_value(v)
                            if cleaned_v is not None:
                                cleaned[k] = cleaned_v
                        return cleaned if cleaned else None
                    if isinstance(value, list):
                        # Limpiar cada elemento de la lista
                        cleaned = []
                        for item in value:
                            cleaned_item = _clean_value(item)
                            if cleaned_item is not None:
                                cleaned.append(cleaned_item)
                        return cleaned if cleaned else None
                    return value
                
                result = {}
                for k, v in data.items():
                    cleaned_v = _clean_value(v)
                    if cleaned_v is not None:
                        # Verificar casos especiales después de limpiar
                        if isinstance(cleaned_v, dict) and not cleaned_v:
                            _logger.info("🗑️ Eliminando campo '%s': dict vacío después de limpieza", k)
                            continue
                        if isinstance(cleaned_v, list) and len(cleaned_v) == 0:
                            _logger.info("🗑️ Eliminando campo '%s': lista vacía después de limpieza", k)
                            continue
                        if isinstance(cleaned_v, str) and not cleaned_v.strip():
                            _logger.info("🗑️ Eliminando campo '%s': string vacío", k)
                            continue
                        result[k] = cleaned_v
                    else:
                        _logger.info("🗑️ Eliminando campo '%s': valor vacío o None", k)
                return result

            # Llamada a la API sin envoltorio "product"
            if publication.tn_product_id:
                payload_put = _strip_empty(product_data)  # sin variants
                _logger.debug("🟦 Payload TiendaNube (PUT producto): %s", payload_put)
                _logger.info("🔍 Campos en payload_put: %s", list(payload_put.keys()))
                if 'weight' in payload_put:
                    _logger.info("   ✅ weight: %s", payload_put['weight'])
                if 'width' in payload_put:
                    _logger.info("   ✅ width: %s", payload_put['width'])
                if 'height' in payload_put:
                    _logger.info("   ✅ height: %s", payload_put['height'])
                if 'depth' in payload_put:
                    _logger.info("   ✅ depth: %s", payload_put['depth'])
                if 'sku' in payload_put:
                    _logger.info("   ✅ sku: %s", payload_put['sku'])
                if 'barcode' in payload_put:
                    _logger.info("   ✅ barcode: %s", payload_put['barcode'])
                endpoint = f'/products/{publication.tn_product_id}'
                _logger.info("📤 Enviando PUT a %s", endpoint)
                response = self._make_request('PUT', endpoint, data=payload_put, tn_config=tn_config)
                product_id = self._extract_tn_product_id(response) or publication.tn_product_id
            else:
                payload_post = _strip_empty(product_data_post)
                
                # VALIDACIÓN CRÍTICA: Si hay variants → SIEMPRE debe haber attributes
                # Esta validación falla ANTES de llamar a la API para evitar error 422
                if 'variants' in payload_post and payload_post.get('variants'):
                    if 'attributes' not in payload_post or not payload_post.get('attributes'):
                        _logger.error("=" * 100)
                        _logger.error("❌ ERROR CRÍTICO: El payload tiene variants pero NO tiene attributes")
                        _logger.error("=" * 100)
                        _logger.error("📋 Payload actual:")
                        _logger.error("   Variants presentes: %s", bool(payload_post.get('variants')))
                        _logger.error("   Attributes presentes: %s", bool(payload_post.get('attributes')))
                        _logger.error("   Campos en payload: %s", list(payload_post.keys()))
                        _logger.error("=" * 100)
                        raise UserError(_("Error: El payload tiene variantes pero no tiene atributos. Esto es obligatorio según la API de TiendaNube. Omitir attributes causa el error 'Variant values should not be repeated'."))
                    
                    _logger.info("✅ Payload final: Producto CON variantes (variants=%d, attributes=%d)", 
                               len(payload_post.get('variants', [])), len(payload_post.get('attributes', [])))
                    _logger.info("   Attributes en payload: %s", payload_post.get('attributes', []))
                elif 'attributes' in payload_post and payload_post.get('attributes'):
                    # Hay attributes y NO hay variantes → eliminar variants vacío si existe
                    if 'variants' in payload_post:
                        _logger.info("🗑️ Eliminando campo 'variants' vacío (hay attributes pero no variantes)")
                        del payload_post['variants']
                    _logger.info("✅ Payload final: Producto SIN variantes (attributes=SÍ, variants=NO)")
                else:
                    # No hay ni variantes ni attributes definidos
                    if 'variants' in payload_post:
                        del payload_post['variants']
                    if 'attributes' in payload_post and not payload_post.get('attributes'):
                        # Si attributes está vacío, eliminarlo
                        del payload_post['attributes']
                        _logger.info("🗑️ Eliminando campo 'attributes' vacío")
                    _logger.info("✅ Payload final: Producto simple (sin variantes ni attributes)")
                
                # VALIDACIÓN FINAL: Verificar que no haya variantes duplicadas en el payload
                # TiendaNube no permite variantes con la misma combinación de valores
                if 'variants' in payload_post and payload_post.get('variants'):
                    variants_to_check = payload_post['variants']
                    _logger.info("🔍 Validación final: Verificando %d variantes por duplicados...", len(variants_to_check))
                    
                    # Normalizar función para comparación (sin ordenar, mantener orden)
                    def normalize_values_for_comparison(variant_dict):
                        """Normaliza los valores de una variante para comparación"""
                        variant_values = variant_dict.get('values', [])
                        if not variant_values:
                            return 'no_values'
                        # Mantener el orden de los valores, pero normalizar cada uno
                        normalized = tuple([
                            str(val.get('es', '')).strip().lower()
                            for val in variant_values
                            if val.get('es', '').strip()
                        ])
                        return normalized if normalized else 'empty_values'
                    
                    seen_combinations = {}
                    duplicates_in_payload = []
                    
                    for idx, variant in enumerate(variants_to_check):
                        values_key = normalize_values_for_comparison(variant)
                        
                        if values_key in seen_combinations:
                            # Duplicado encontrado
                            original_idx = seen_combinations[values_key]
                            _logger.error("❌ DUPLICADO EN PAYLOAD:")
                            _logger.error("   Variante #%d: values=%s, variant=%s", 
                                        idx + 1, values_key, variant.get('values', 'NO VALUES'))
                            _logger.error("   Duplicada de variante #%d: values=%s, variant=%s", 
                                        original_idx + 1, values_key, variants_to_check[original_idx].get('values', 'NO VALUES'))
                            duplicates_in_payload.append((idx + 1, original_idx + 1, variant, values_key))
                        else:
                            seen_combinations[values_key] = idx
                            _logger.debug("✅ Variante #%d única: values=%s", idx + 1, values_key)
                    
                    if duplicates_in_payload:
                        _logger.error("❌ SE ENCONTRARON %d VARIANTES DUPLICADAS EN EL PAYLOAD FINAL", len(duplicates_in_payload))
                        for dup in duplicates_in_payload:
                            _logger.error("   - Variante #%d duplicada de #%d: values=%s", dup[0], dup[1], dup[3])
                        
                        # Eliminar duplicados del payload, manteniendo solo la primera ocurrencia
                        unique_variants = []
                        seen_combinations_clean = set()
                        for variant in variants_to_check:
                            values_key = normalize_values_for_comparison(variant)
                            if values_key not in seen_combinations_clean:
                                seen_combinations_clean.add(values_key)
                                unique_variants.append(variant)
                            else:
                                _logger.warning("⚠️ Omitiendo variante duplicada: values=%s", values_key)
                        
                        payload_post['variants'] = unique_variants
                        _logger.warning("⚠️ Se eliminaron %d variantes duplicadas. Quedan %d variantes únicas.", 
                                      len(variants_to_check) - len(unique_variants), len(unique_variants))
                    else:
                        _logger.info("✅ No se encontraron duplicados en el payload final. Todas las variantes son únicas.")
                
                # VALIDACIÓN CRÍTICA: Verificar que todos los precios sean numéricos ANTES de enviar
                if 'variants' in payload_post and payload_post.get('variants'):
                    _logger.info("🔍 Validando tipos de datos en variantes antes de enviar...")
                    variants_invalid_price = []
                    for idx, variant_dict in enumerate(payload_post.get('variants', [])):
                        price = variant_dict.get('price')
                        if price is None:
                            variants_invalid_price.append({
                                'index': idx + 1,
                                'variant': variant_dict,
                                'error': 'price es None'
                            })
                            _logger.error("❌ Variante #%d: price es None", idx + 1)
                        elif not isinstance(price, (int, float)):
                            variants_invalid_price.append({
                                'index': idx + 1,
                                'variant': variant_dict,
                                'error': f'price es {type(price).__name__} en lugar de int/float',
                                'price_value': price,
                                'price_type': type(price).__name__
                            })
                            _logger.error("❌ Variante #%d: price=%s (tipo: %s) - DEBE ser int o float", 
                                        idx + 1, price, type(price).__name__)
                        else:
                            # Precio válido, loguear confirmación
                            _logger.info("✅ Variante #%d: price=%s (tipo: %s) - VÁLIDO", 
                                       idx + 1, price, type(price).__name__)
                    
                    if variants_invalid_price:
                        error_msg = f"ERROR CRÍTICO: {len(variants_invalid_price)} variante(s) con precio inválido:\n"
                        for inv in variants_invalid_price:
                            error_msg += f"  - Variante #{inv['index']}: {inv['error']}"
                            if 'price_value' in inv:
                                error_msg += f" (valor: {inv['price_value']}, tipo: {inv['price_type']})"
                            error_msg += "\n"
                        _logger.error("=" * 100)
                        _logger.error(error_msg)
                        _logger.error("=" * 100)
                        raise UserError(_(f"Error: {len(variants_invalid_price)} variante(s) tienen precio inválido (debe ser numérico). Detalles en logs."))
                    else:
                        _logger.info("✅ Validación de precios exitosa: todas las variantes tienen precio numérico válido")
                
                _logger.debug("🟦 Payload TiendaNube (POST producto): %s", payload_post)
                _logger.info("🔍 Campos en payload_post: %s", list(payload_post.keys()))
                if 'variants' in payload_post:
                    _logger.info("🔍 Variantes en payload_post: %d", len(payload_post.get('variants', [])))
                    if payload_post.get('variants'):
                        _logger.debug("🔍 Primera variante en payload_post: %s", payload_post['variants'][0])
                        # Log DETALLADO de todas las variantes antes de enviar
                        _logger.info("=" * 100)
                        _logger.debug("📋 LOG DETALLADO DE VARIANTES (DEBUG): %d variantes", len(payload_post.get('variants', [])))
                        for idx, variant in enumerate(payload_post.get('variants', [])):
                            _logger.debug("📦 VARIANTE #%d: %s", idx + 1, json.dumps(variant, indent=2, ensure_ascii=False))
                
                endpoint = '/products'
                _logger.info("📤 Enviando POST a %s", endpoint)
                # El JSON completo se mostrará en _make_request (evitar duplicación)
                response = self._make_request('POST', endpoint, data=payload_post, tn_config=tn_config)
                _logger.info(
                    "[TN BIND] POST /products pub=%s extract de response type=%s",
                    publication.id, type(response).__name__,
                )
                product_id = self._extract_tn_product_id(response)
                if not product_id:
                    product_id = self._lookup_tn_product_id_by_handle(
                        publication.handle or handle, tn_config=tn_config,
                    )

            if not product_id:
                _logger.error("=" * 100)
                _logger.error("❌ ERROR: NO SE OBTUVO PRODUCT_ID EN LA RESPUESTA")
                _logger.error("=" * 100)
                _logger.error("📋 Información de la publicación:")
                _logger.error("   ID Odoo: %s", publication.id)
                _logger.error("   Título: %s", publication.title)
                _logger.error("   Handle: %s", publication.handle)
                _logger.error("")
                _logger.error("📋 Respuesta recibida:")
                _logger.error("   Tipo: %s", type(response).__name__)
                try:
                    _logger.error("   Contenido: %s", json.dumps(response, indent=2, ensure_ascii=False) if isinstance(response, (dict, list)) else str(response))
                except:
                    _logger.error("   Contenido: %s", str(response))
                _logger.error("=" * 100)
                raise UserError(_("No se pudo obtener el ID del producto creado/actualizado en TiendaNube. Respuesta: %s") % str(response))

            _logger.info("[TN BIND] llamando bind pub=%s tn_id=%s", publication.id, product_id)
            bound = self._bind_publication_to_tn_product(publication, product_id, response=response)
            _logger.info("[TN BIND] bind result=%s pub.tn_product_id=%s", bound, publication.tn_product_id)

            # Refrescar IDs de variantes desde API y actualizar stocks/precios vía endpoint dedicado
            try:
                _logger.info("🔄 Refrescando IDs de variantes desde API para producto %s", product_id)
                self._refresh_variant_ids_from_api(publication, product_id)
                
                # Verificar si las variantes tienen tn_variant_id después del refresh
                variants_with_id = [v for v in publication.variant_ids if v.tn_variant_id]
                _logger.info("🔍 Variantes con tn_variant_id después del refresh: %d de %d", 
                            len(variants_with_id), len(publication.variant_ids))
                for v in publication.variant_ids:
                    _logger.info("   - Variante '%s': tn_variant_id=%s, sku='%s'", 
                                v.name or 'sin nombre', v.tn_variant_id, v.sku)
                
                # Actualizar variantes individualmente (sin precio desde Odoo)
                _logger.info("🔄 Actualizando variantes individualmente...")
                self._update_variants_individually(publication, product_id, sync_stock=True, sync_price=False)
            except Exception as e:
                _logger.error("❌ Error refrescando/actualizando variantes individualmente: %s", e, exc_info=True)

            # Subir imágenes (si hay)
            try:
                _logger.info("🖼️ Iniciando exportación de imágenes para producto %s", product_id)
                self._export_images(publication, product_id)
                _logger.info("✅ Exportación de imágenes completada")
            except Exception as e_img:
                _logger.error("❌ Error subiendo imágenes: %s", e_img, exc_info=True)

            # Exportar campos personalizados (Product Custom Fields)
            try:
                _logger.info("🧩 Iniciando exportación de campos personalizados para producto %s", product_id)
                self._export_custom_fields(publication, product_id)
                _logger.info("✅ Exportación de campos personalizados completada")
            except Exception as e_cf:
                _logger.error("❌ Error exportando campos personalizados: %s", e_cf, exc_info=True)

            duration_ms = int((time.time() - export_start) * 1000)
            try:
                self._tn_audit_sync(
                    publication,
                    'export',
                    'ok',
                    tn_config=tn_config,
                    value_before='',
                    value_after=str(product_id),
                    http_status=200,
                    duration_ms=duration_ms,
                    force_log=True,
                )
            except Exception as e_audit:
                _logger.warning("Export OK pero falló el log de auditoría: %s", e_audit)
            return {
                'success': True,
                'product_id': int(product_id),
            }
        except UserError as e:
            duration_ms = int((time.time() - export_start) * 1000)
            self._tn_audit_sync(
                publication,
                'export',
                'error',
                tn_config=tn_config,
                value_before=str(publication.tn_product_id or ''),
                value_after='',
                http_status=0,
                duration_ms=duration_ms,
                error_message=(str(e) or '')[:65535],
                force_log=True,
            )
            # Re-lanzar UserError sin modificar (ya tiene mensaje claro)
            raise
        except Exception as e:
            duration_ms = int((time.time() - export_start) * 1000)
            self._tn_audit_sync(
                publication,
                'export',
                'error',
                tn_config=tn_config,
                value_before=str(publication.tn_product_id or ''),
                value_after='',
                http_status=0,
                duration_ms=duration_ms,
                error_message=(str(e) or '')[:65535],
                force_log=True,
            )
            _logger.error("=" * 100)
            _logger.error("❌ ERROR AL EXPORTAR PUBLICACIÓN")
            _logger.error("=" * 100)
            _logger.error("📋 Información de la publicación:")
            _logger.error("   ID Odoo: %s", publication.id)
            _logger.error("   Título: %s", publication.title)
            _logger.error("   Handle: %s", publication.handle)
            _logger.error("   TN Product ID: %s", publication.tn_product_id)
            _logger.error("   Tiene variantes: %s (%d)", bool(publication.variant_ids), len(publication.variant_ids))
            if publication.variant_ids:
                _logger.error("   Variantes:")
                for idx, variant in enumerate(publication.variant_ids, 1):
                    _logger.error("      %d. %s (SKU: %s, Price: %s, Stock: %s)", 
                                idx, variant.name or 'sin nombre', variant.sku, variant.price, variant.stock)
            _logger.error("   Tiene atributos: %s", bool(publication.variant_ids))
            _logger.error("")
            _logger.error("📋 Información del error:")
            _logger.error("   Tipo: %s", type(e).__name__)
            _logger.error("   Mensaje: %s", str(e))
            _logger.error("")
            _logger.exception("📚 Stack trace completo:")
            _logger.error("=" * 100)
            raise

    def _prepare_variants(self, publication):
        """
        Construye la lista 'variants' siguiendo EXACTAMENTE el formato de la documentación de TiendaNube:
        https://tiendanube.github.io/api-documentation/resources/product-variant
        
        Formato según documentación:
        - price: string (ej: "25.00")
        - stock: integer o null
        - stock_management: boolean
        - weight: string o null (en kg)
        - width: string o null (en cm)
        - height: string o null (en cm)
        - depth: string o null (en cm)
        - sku: string o null
        - values: array de objetos multilanguage (ej: [{"es": "verde"}])
        - barcode: string o null
        """
        variants = []
        
        if not publication.variant_ids:
            _logger.info("🔍 No hay variantes definidas en la publicación")
            return variants
        
        for variant in publication.variant_ids:
            # Construir variante siguiendo formato exacto de la documentación (sin precio desde Odoo)
            v = {}
            
            # stock: número entero (stock_management se infiere automáticamente por TiendaNube)
            if variant.stock is not None:
                stock_value = int(variant.stock)
                if stock_value < 0:
                    stock_value = 0
                v["stock"] = stock_value
            else:
                v["stock"] = 0
            
            # sku: obligatorio, generar si no existe
            sku = (variant.sku or '').strip() if variant.sku else ''
            if not sku:
                # Intentar desde Odoo
                if variant.odoo_variant_id and variant.odoo_variant_id.default_code:
                    sku = variant.odoo_variant_id.default_code.strip()
                else:
                    # Generar determinístico
                    handle_base = (publication.handle or publication.title or 'PROD').strip().upper().replace(' ', '-')
                    variant_suffix = str(variant.id) if variant.id else str(hash(variant.name or ''))[:8]
                    sku = f"{handle_base}-{variant_suffix}".upper()
            v["sku"] = sku
            _logger.info("✅ SKU asignado a variante '%s': '%s'", variant.name or 'sin nombre', sku)
            
            # values: array de objetos multilanguage
            # IMPORTANTE: Solo incluir variantes que tengan al menos un valor de atributo
            values = []
            if variant.option1_value and variant.option1_value.strip():
                values.append({'es': variant.option1_value.strip()})
            if variant.option2_value and variant.option2_value.strip():
                values.append({'es': variant.option2_value.strip()})
            if variant.option3_value and variant.option3_value.strip():
                values.append({'es': variant.option3_value.strip()})
            
            # Si la variante no tiene valores de atributos, es un producto simple (sin variantes)
            # No incluir en el payload de variantes
            if not values:
                _logger.info("⚠️ Saltando variante sin valores de atributos (producto simple): %s", variant.name or 'sin nombre')
                continue
            
            # Si tiene valores, agregarlos al payload
            v["values"] = values
            
            # weight: string o null (en kg)
            weight_value = variant.weight if variant.weight and variant.weight > 0 else None
            if not weight_value and publication.weight and publication.weight > 0:
                weight_value = publication.weight
            if weight_value and weight_value > 0:
                v["weight"] = f"{weight_value:.2f}"
            
            # width: string o null (en cm)
            width_value = variant.width if variant.width and variant.width > 0 else None
            if not width_value and publication.width and publication.width > 0:
                width_value = publication.width
            if width_value and width_value > 0:
                v["width"] = f"{width_value:.2f}"
            
            # height: string o null (en cm)
            height_value = variant.height if variant.height and variant.height > 0 else None
            if not height_value and publication.height and publication.height > 0:
                height_value = publication.height
            if height_value and height_value > 0:
                v["height"] = f"{height_value:.2f}"
            
            # depth: string o null (en cm)
            depth_value = variant.depth if variant.depth and variant.depth > 0 else None
            if not depth_value and publication.depth and publication.depth > 0:
                depth_value = publication.depth
            if depth_value and depth_value > 0:
                v["depth"] = f"{depth_value:.2f}"
            
            # barcode: string o null
            variant_barcode = (getattr(variant, 'barcode', None) or '').strip() if getattr(variant, 'barcode', None) else ''
            if not variant_barcode and publication.barcode:
                variant_barcode = (publication.barcode or '').strip() if publication.barcode else ''
            if variant_barcode:
                v["barcode"] = variant_barcode
            
            variants.append(v)
            _logger.debug("✅ Variante preparada (formato TiendaNube): %s", json.dumps(v, indent=2, ensure_ascii=False))
        
        # Validar duplicados (valores únicos)
        if variants:
            seen_values = set()
            unique_variants = []
            for variant_dict in variants:
                # Crear clave única basada en values
                variant_values = variant_dict.get('values', [])
                if variant_values:
                    values_key = tuple(sorted([
                        str(val.get('es', '')).strip().lower() 
                        for val in variant_values 
                        if val.get('es', '').strip()
                    ]))
                    if values_key in seen_values:
                        _logger.warning("⚠️ Variante duplicada omitida: values=%s", values_key)
                        continue
                    seen_values.add(values_key)
                unique_variants.append(variant_dict)
            
            if len(unique_variants) < len(variants):
                _logger.info("✅ Eliminadas %d variantes duplicadas. Quedan %d únicas", 
                           len(variants) - len(unique_variants), len(unique_variants))
            variants = unique_variants
        
        return variants

    def _prepare_attributes(self, publication):
        """
        Prepara la sección attributes con SOLO los nombres de los atributos.
        
        Formato según documentación TiendaNube:
        "attributes": [
            {"es": "Size"},
            {"es": "Color"}
        ]
        
        IMPORTANTE: 
        - attributes solo contiene los NOMBRES de los atributos (objetos multilanguage)
        - Los VALORES de los atributos van en variants[].values
        - Solo incluir atributos que tienen valores en las variantes
        """
        attributes = []

        # Reunir nombres únicos de atributos desde las variantes
        # Solo incluir opciones que tienen valores reales (no vacíos)
        attribute_names = set()
        
        for variant in publication.variant_ids:
            # Solo procesar opciones que tienen valores (no vacíos)
            if variant.option1_value and variant.option1_value.strip():
                attr_name = variant.option1_name or 'Opción 1'
                if attr_name and attr_name.strip():
                    attribute_names.add(attr_name.strip())
            
            if variant.option2_value and variant.option2_value.strip():
                attr_name = variant.option2_name or 'Opción 2'
                if attr_name and attr_name.strip():
                    attribute_names.add(attr_name.strip())
            
            if variant.option3_value and variant.option3_value.strip():
                attr_name = variant.option3_name or 'Opción 3'
                if attr_name and attr_name.strip():
                    attribute_names.add(attr_name.strip())

        # Convertir a lista en formato TiendaNube: solo nombres, sin values
        # Formato: [{"es": "Size"}, {"es": "Color"}]
        for attr_name in sorted(attribute_names):  # Ordenar para consistencia
            attributes.append({'es': attr_name})
            _logger.info("📋 Attribute agregado: '%s'", attr_name)

        _logger.info("📋 Attributes preparados: %d attributes (solo nombres)", len(attributes))
        if attributes:
            attr_names_list = [attr.get('es', 'N/A') for attr in attributes]
            _logger.info("   - Nombres: %s", attr_names_list)
        return attributes

    # -------------------------------------------------------------------------
    # PRODUCT CUSTOM FIELDS (Campos personalizados)
    # Basado en la documentación oficial:
    # https://tiendanube.github.io/api-documentation/resources/products/custom-fields
    # -------------------------------------------------------------------------

    def _get_all_custom_fields(self):
        """Obtiene todos los custom fields globales de TiendaNube (products/custom-fields)."""
        try:
            response = self._make_request('GET', '/products/custom-fields', retry=False)
            if isinstance(response, list):
                return response
            return []
        except Exception as e:
            _logger.warning("⚠️ No se pudieron obtener products/custom-fields: %s", e)
            return []

    def _get_or_create_custom_field_id(self, name, tn_config=None):
        """Obtiene (o crea) el ID de un custom field global por nombre."""
        if not name:
            return None

        # Cache simple en contexto para evitar múltiples llamadas
        cache_key = '_tn_custom_fields_cache'
        if cache_key not in self.env.context:
            all_fields = self._get_all_custom_fields()
            cache = {cf.get('name'): cf.get('id') for cf in all_fields if cf.get('id') and cf.get('name')}
            self = self.with_context(**{cache_key: cache})
        else:
            cache = dict(self.env.context.get(cache_key) or {})

        if name in cache and cache.get(name):
            return cache[name]

        # Crear nuevo custom field global (value_type text)
        payload = {
            "name": name,
            "description": "",
            "value_type": "text",
            "read_only": False,
            "values": [],
        }
        try:
            _logger.info("🧩 Creando custom field global en TiendaNube: %s", name)
            response = self._make_request('POST', '/products/custom-fields', data=payload, retry=False, tn_config=tn_config)
            cf_id = response.get('id') if isinstance(response, dict) else None
            if cf_id:
                cache[name] = cf_id
                self = self.with_context(**{cache_key: cache})
                return cf_id
        except Exception as e:
            _logger.error("❌ Error creando custom field global '%s': %s", name, e)
        return None

    def _import_custom_fields(self, publication, product_id):
        """Importa los campos personalizados asociados a un producto desde TiendaNube."""
        if not product_id:
            return

        try:
            response = self._make_request('GET', f'/products/{product_id}/custom-fields', retry=False)
            if not response or not isinstance(response, list):
                _logger.info("🧩 Producto %s sin custom fields asociados", product_id)
                # Limpiar campos locales si existían
                publication.attribute_ids.unlink()
                return

            _logger.info("🧩 Custom fields recibidos para producto %s: %s", product_id, response)

            # Limpiar campos existentes y recrear según TiendaNube
            publication.attribute_ids.unlink()

            for seq, cf in enumerate(response, start=1):
                try:
                    cf_id = cf.get('id')
                    name = cf.get('name') or ''
                    value = cf.get('value') or ''
                    if not name:
                        continue
                    
                    # Solo importar campos que tienen valor (no vacío)
                    # Cada publicación tiene sus propios campos, no queremos campos vacíos
                    value_stripped = value.strip() if isinstance(value, str) else str(value) if value else ""
                    if not value_stripped:
                        _logger.debug("🧩 Campo personalizado '%s' (ID: %s) sin valor en TiendaNube, omitiendo importación", name, cf_id)
                        continue
                    
                    publication.attribute_ids.create({
                        'publication_id': publication.id,
                        'name': name,
                        'value': value_stripped,
                        'sequence': seq * 10,
                        'tn_custom_field_id': cf_id,
                    })
                    _logger.debug("🧩 Campo personalizado '%s' (ID: %s) importado con valor: '%s'", name, cf_id, value_stripped)
                except Exception as e_cf:
                    _logger.warning("⚠️ Error creando campo personalizado local para producto %s: %s", product_id, e_cf)
        except Exception as e:
            _logger.warning("⚠️ Error obteniendo custom fields para producto %s: %s", product_id, e)

    def _export_custom_fields(self, publication, product_id):
        """
        Exporta los campos personalizados de un producto hacia TiendaNube.
        
        IMPORTANTE: Esta función sincroniza completamente los custom fields:
        - Envía los campos de la publicación actual con valor
        - Elimina los campos que existen en TiendaNube pero no están en la publicación actual
        """
        if not product_id:
            _logger.warning("⚠️ No se puede exportar custom fields: product_id no proporcionado")
            return
        
        # Obtener la cuenta de la publicación
        tn_config = publication.tn_config_id if publication.tn_config_id else None

        # Paso 1: Obtener custom fields existentes en TiendaNube
        try:
            tn_fields = self._make_request('GET', f'/products/{product_id}/custom-fields', retry=False, tn_config=tn_config)
            tn_fields = tn_fields if isinstance(tn_fields, list) else []
            _logger.info("🧩 Custom fields existentes en TiendaNube para producto %s: %d campos", product_id, len(tn_fields))
        except Exception as e:
            _logger.warning("⚠️ No se pudieron obtener custom fields existentes para producto %s: %s", product_id, e)
            tn_fields = []

        # Mapear IDs existentes en TiendaNube
        existing_ids = {cf.get('id') for cf in tn_fields if cf.get('id')}
        _logger.info("🧩 IDs de custom fields existentes en TiendaNube: %s", existing_ids)

        # Paso 2: Asegurar que cada atributo local tenga su tn_custom_field_id
        for attr in publication.attribute_ids:
            if not attr.tn_custom_field_id:
                cf_id = self._get_or_create_custom_field_id(attr.name.strip(), tn_config=tn_config)
                if cf_id:
                    attr.tn_custom_field_id = cf_id
                    _logger.debug("🧩 Asignado tn_custom_field_id=%s al campo '%s'", cf_id, attr.name)
                else:
                    _logger.warning("⚠️ No se pudo obtener/crear custom field global para '%s'. Omitiendo.", attr.name)

        # Paso 3: Construir mapeo de campos locales con valor
        local_fields_with_value = {}
        for attr in publication.attribute_ids:
            if not attr.tn_custom_field_id:
                continue
            
            # Solo incluir campos con valor (no vacío)
            attr_value = attr.value or ""
            attr_value_stripped = attr_value.strip() if isinstance(attr_value, str) else str(attr_value) if attr_value else ""
            
            if attr_value_stripped:
                local_fields_with_value[attr.tn_custom_field_id] = {
                    'id': attr.tn_custom_field_id,
                    'name': attr.name,
                    'value': attr_value_stripped
                }
                _logger.info("🧩 Campo local '%s' (ID: %s) con valor: '%s'", 
                            attr.name, attr.tn_custom_field_id, attr_value_stripped)
            else:
                _logger.info("🧩 Campo local '%s' (ID: %s) sin valor - NO se incluirá", 
                            attr.name, attr.tn_custom_field_id)

        # Paso 4: Construir payload completo
        # - Incluir campos locales con valor
        # - Eliminar campos que existen en TiendaNube pero no están en la publicación actual
        values_payload = []
        
        # 4a) Agregar campos locales con valor
        for cf_id, field_data in local_fields_with_value.items():
            values_payload.append({
                "id": field_data['id'],
                "value": field_data['value'],
            })
            _logger.debug("🧩 Agregando campo local al payload: %s = '%s'", field_data['name'], field_data['value'])
        
        # 4b) Eliminar campos que existen en TiendaNube pero no están en la publicación actual
        local_ids = set(local_fields_with_value.keys())
        fields_to_remove = existing_ids - local_ids
        
        for cf_id in fields_to_remove:
            # Buscar el nombre del campo para el log
            field_name = next((cf.get('name', '') for cf in tn_fields if cf.get('id') == cf_id), f'ID_{cf_id}')
            values_payload.append({
                "id": cf_id,
                "value": None,  # value = None elimina la asociación según docs de TiendaNube
            })
            _logger.info("🧩 Campo '%s' (ID: %s) existe en TiendaNube pero no en publicación actual - ELIMINANDO", 
                        field_name, cf_id)

        # Log detallado antes de enviar
        _logger.info("🧩 Resumen de campos personalizados para producto %s:", product_id)
        _logger.info("   - Campos locales con valor: %d", len(local_fields_with_value))
        _logger.info("   - Campos a eliminar de TiendaNube: %d", len(fields_to_remove))
        _logger.info("   - Total en payload: %d", len(values_payload))
        
        if not values_payload:
            _logger.info("🧩 No hay campos personalizados para sincronizar para producto %s (no hay campos con valor ni campos a eliminar)", product_id)
            return

        _logger.info("🧩 Custom fields para producto %s: %d campos", product_id, len(values_payload))
        try:
            json_str_custom = json.dumps(values_payload, indent=2, ensure_ascii=False)
            _logger.debug("📤 JSON Payload (PUT custom fields): %s", json_str_custom)
        except Exception as e:
            _logger.warning("⚠️ No se pudo serializar JSON para log: %s", e)

        # Paso 5: Enviar payload completo en un único PUT
        try:
            # PUT /products/{id}/custom-fields/values sobrescribe las asociaciones de ese producto
            _logger.info("📤 Enviando PUT a /products/%s/custom-fields/values", product_id)
            # El JSON completo también se mostrará en _make_request
            self._make_request('PUT', f'/products/{product_id}/custom-fields/values', data=values_payload, retry=False, tn_config=tn_config)
            _logger.info("✅ Campos personalizados sincronizados correctamente para producto %s", product_id)
        except Exception as e:
            _logger.error("❌ Error sincronizando campos personalizados para producto %s: %s", product_id, e, exc_info=True)
            raise

    def _update_variants_individually(self, publication, product_id, sync_stock=True, sync_price=False):
        """
        Actualiza variantes ya existentes en TiendaNube usando:
        PUT /products/{product_id}/variants

        Odoo nunca envía precio a TiendaNube (sync_price se ignora; siempre False).
        """
        sync_price = False
        variants_payload = []
        
        # Obtener la cuenta de la publicación
        tn_config = publication.tn_config_id if publication.tn_config_id else None

        def _tn_op_label():
            if sync_stock:
                return 'stock_update'
            return 'price_update'

        def _tn_fmt_variant_audit(p):
            if not isinstance(p, dict):
                return str(p)[:65000]
            snap = {}
            for key in ('id', 'stock', 'price'):
                if key in p:
                    snap[key] = p[key]
            try:
                return json.dumps(snap, ensure_ascii=False)[:65000]
            except Exception:
                return str(snap)[:65000]

        def _tn_fmt_bulk_audit(rows):
            try:
                brief = [{k: r.get(k) for k in ('id', 'stock', 'price') if k in r} for r in rows]
                return json.dumps(brief, ensure_ascii=False)[:65000]
            except Exception:
                return str(len(rows))

        _logger.info("🔄 Preparando actualización de variantes para producto %s", product_id)
        _logger.info("🔍 Variantes locales: %d", len(publication.variant_ids))
        if tn_config:
            _logger.info("🔑 Usando cuenta: %s (ID: %d)", tn_config.name, tn_config.id)
        
        # Verificar si hay variantes con ID de TiendaNube
        variants_with_tn_id = [v for v in publication.variant_ids if v.tn_variant_id]
        
        # Si hay variantes en Odoo pero ninguna tiene tn_variant_id, intentar rellenar desde API
        if publication.variant_ids and not variants_with_tn_id:
            _logger.info("📥 Variantes sin ID de TiendaNube: intentando mapear desde API...")
            self._refresh_variant_ids_from_api(publication, product_id, tn_config=tn_config)
            publication.variant_ids.invalidate_recordset(['tn_variant_id'])
            variants_with_tn_id = [v for v in publication.variant_ids if v.tn_variant_id]
            if variants_with_tn_id:
                _logger.info("✅ Se obtuvieron %d tn_variant_id desde la API", len(variants_with_tn_id))
        
        # Publicación "por variante" (una fila por variante TN): actualizar solo esta variante
        # con PUT por ID. Si hiciéramos PUT /variants con una sola variante, TN reemplazaría
        # todas las variantes del producto y borraría el resto.
        if publication.main_tn_variant_id and variants_with_tn_id:
            _logger.info("📦 Publicación por variante (main_tn_variant_id=%s): actualizando solo esta variante por ID",
                         publication.main_tn_variant_id)
            for variant in publication.variant_ids:
                if not variant.tn_variant_id:
                    continue
                payload = {
                    "id": variant.tn_variant_id,
                    "values": [],
                }
                if variant.option1_value:
                    payload["values"].append({'es': variant.option1_value})
                if variant.option2_value:
                    payload["values"].append({'es': variant.option2_value})
                if variant.option3_value:
                    payload["values"].append({'es': variant.option3_value})
                if sync_stock and variant.stock is not None:
                    payload["stock"] = int(variant.stock)
                endpoint = f"/products/{product_id}/variants/{variant.tn_variant_id}"
                _logger.info("📤 PUT %s (solo price/stock)", endpoint)
                start_put = time.time()
                try:
                    self._make_request('PUT', endpoint, data=payload, retry=False, tn_config=tn_config)
                except Exception as e:
                    duration_ms = int((time.time() - start_put) * 1000)
                    self._tn_audit_sync(
                        publication,
                        _tn_op_label(),
                        'error',
                        tn_config=tn_config,
                        value_before='-',
                        value_after=_tn_fmt_variant_audit(payload),
                        http_status=0,
                        duration_ms=duration_ms,
                        error_message=(str(e) or '')[:65535],
                        force_log=True,
                    )
                    raise
                duration_ms = int((time.time() - start_put) * 1000)
                self._tn_audit_sync(
                    publication,
                    _tn_op_label(),
                    'ok',
                    tn_config=tn_config,
                    value_before='-',
                    value_after=_tn_fmt_variant_audit(payload),
                    http_status=200,
                    duration_ms=duration_ms,
                    force_log=True,
                )
            return

        # Si no hay variantes definidas O si hay variantes pero ninguna tiene tn_variant_id,
        # debemos actualizar la variante virtual
        # Según documentación TiendaNube: PUT /products/{product_id}/variants/{id}
        if not publication.variant_ids or not variants_with_tn_id:
            if not publication.variant_ids:
                _logger.info("📦 No hay variantes en Odoo, actualizando variante virtual directamente")
            else:
                _logger.info("📦 Hay variantes pero ninguna tiene ID de TiendaNube, actualizando variante virtual directamente")
            try:
                response = self._make_request('GET', f'/products/{product_id}', retry=False, tn_config=tn_config)
                if response and isinstance(response, dict):
                    api_variants = response.get('variants') or []
                    if len(api_variants) == 1:
                        virtual_variant_id = api_variants[0].get('id')
                        _logger.info("✅ Variante virtual encontrada: ID %s", virtual_variant_id)
                        
                        # Construir payload con valores del producto
                        virtual_payload = {
                            "id": virtual_variant_id,
                            "values": [],  # REQUERIDO por API TiendaNube (array vacío si no hay atributos)
                        }
                        
                        # Stock si hay variantes con stock
                        if publication.weight and publication.weight > 0:
                            virtual_payload["weight"] = str(publication.weight)
                            _logger.info("   ✅ weight: %s kg", publication.weight)
                        if publication.width and publication.width > 0:
                            virtual_payload["width"] = str(publication.width)
                            _logger.info("   ✅ width: %s cm", publication.width)
                        if publication.height and publication.height > 0:
                            virtual_payload["height"] = str(publication.height)
                            _logger.info("   ✅ height: %s cm", publication.height)
                        if publication.depth and publication.depth > 0:
                            virtual_payload["depth"] = str(publication.depth)
                            _logger.info("   ✅ depth: %s cm", publication.depth)
                        if publication.sku:
                            virtual_payload["sku"] = publication.sku.strip()
                            _logger.info("   ✅ sku: '%s'", publication.sku.strip())
                        if publication.barcode:
                            virtual_payload["barcode"] = publication.barcode.strip()
                            _logger.info("   ✅ barcode: '%s'", publication.barcode.strip())
                        
                        # Stock si hay variantes con stock
                        # Solo actualizar si sync_stock es True
                        if sync_stock:
                            if publication.variant_ids and publication.variant_ids[0].stock is not None:
                                virtual_payload["stock"] = int(publication.variant_ids[0].stock)
                                _logger.info("   ✅ stock: %d", int(publication.variant_ids[0].stock))
                        
                        _logger.info("📦 Payload variante virtual: %s", virtual_payload)
                        
                        try:
                            _logger.debug("📤 Payload variante virtual: %s", json.dumps(virtual_payload, indent=2, ensure_ascii=False))
                        except Exception as e:
                            _logger.warning("⚠️ No se pudo serializar JSON para log: %s", e)
                        endpoint = f"/products/{product_id}/variants/{virtual_variant_id}"
                        _logger.info("📤 Enviando PUT a %s", endpoint)
                        # El JSON completo también se mostrará en _make_request
                        start_put = time.time()
                        try:
                            response = self._make_request('PUT', endpoint, data=virtual_payload, retry=False, tn_config=tn_config)
                        except Exception as e:
                            duration_ms = int((time.time() - start_put) * 1000)
                            self._tn_audit_sync(
                                publication,
                                _tn_op_label(),
                                'error',
                                tn_config=tn_config,
                                value_before='-',
                                value_after=_tn_fmt_variant_audit(virtual_payload),
                                http_status=0,
                                duration_ms=duration_ms,
                                error_message=(str(e) or '')[:65535],
                                force_log=True,
                            )
                            raise
                        duration_ms = int((time.time() - start_put) * 1000)
                        self._tn_audit_sync(
                            publication,
                            _tn_op_label(),
                            'ok',
                            tn_config=tn_config,
                            value_before='-',
                            value_after=_tn_fmt_variant_audit(virtual_payload),
                            http_status=200,
                            duration_ms=duration_ms,
                            force_log=True,
                        )
                        _logger.info("✅ Variante virtual actualizada exitosamente. Respuesta: %s", response)
                        return
                    else:
                        _logger.warning("⚠️ Hay %d variantes en TiendaNube, no es una variante virtual única", len(api_variants))
            except Exception as e:
                _logger.error("❌ Error actualizando variante virtual: %s", e, exc_info=True)
            return

        for variant in publication.variant_ids:
            _logger.info("🔍 Procesando variante: nombre='%s', tn_variant_id=%s, sku='%s'", 
                        variant.name or 'sin nombre', variant.tn_variant_id, variant.sku)
            
            if not variant.tn_variant_id:
                _logger.warning("⚠️ Variante '%s' no tiene tn_variant_id, omitiendo", variant.name or 'sin nombre')
                continue  # sin ID remoto, no podemos actualizarla

            sku_value = variant.sku or ''

            v = {
                "id": variant.tn_variant_id,
            }

            # Opciones (valores) multilenguaje - REQUERIDO según API TiendaNube
            values = []
            if variant.option1_value:
                values.append({'es': variant.option1_value})
            if variant.option2_value:
                values.append({'es': variant.option2_value})
            if variant.option3_value:
                values.append({'es': variant.option3_value})
            
            # Si no hay valores, enviar array vacío (requerido por API)
            v["values"] = values
            if values:
                _logger.info("   ✅ values agregados: %s", values)
            else:
                _logger.info("   ✅ values agregado: [] (vacío, requerido por API)")

            # SKU opcional, solo si tiene valor
            if sku_value:
                v["sku"] = sku_value
                _logger.info("   ✅ SKU agregado: '%s'", sku_value)
            elif publication.sku:
                v["sku"] = publication.sku.strip()
                _logger.info("   ✅ SKU desde producto: '%s'", publication.sku.strip())

            # Dimensiones físicas (peso en kg, dimensiones en cm)
            # Si la variante tiene valores, usarlos; si no, usar valores del producto
            weight_value = variant.weight if variant.weight else (publication.weight if publication.weight else None)
            width_value = variant.width if variant.width else (publication.width if publication.width else None)
            height_value = variant.height if variant.height else (publication.height if publication.height else None)
            depth_value = variant.depth if variant.depth else (publication.depth if publication.depth else None)
            
            if weight_value and weight_value > 0:
                v["weight"] = str(weight_value)
                _logger.info("   ✅ weight agregado: %s kg", weight_value)
            if width_value and width_value > 0:
                v["width"] = str(width_value)
                _logger.info("   ✅ width agregado: %s cm", width_value)
            if height_value and height_value > 0:
                v["height"] = str(height_value)
                _logger.info("   ✅ height agregado: %s cm", height_value)
            if depth_value and depth_value > 0:
                v["depth"] = str(depth_value)
                _logger.info("   ✅ depth agregado: %s cm", depth_value)
            
            # Código de barras (barcode) - si la variante no tiene, usar del producto
            variant_barcode = getattr(variant, 'barcode', None) or ''
            if not variant_barcode and publication.barcode:
                variant_barcode = publication.barcode.strip()
            if variant_barcode:
                v["barcode"] = variant_barcode
                _logger.info("   ✅ barcode agregado: '%s'", variant_barcode)

            # Opciones (valores) multilenguaje - REQUERIDO por API TiendaNube (incluso si está vacío)
            values = []
            if variant.option1_value:
                values.append({'es': variant.option1_value})
            if variant.option2_value:
                values.append({'es': variant.option2_value})
            if variant.option3_value:
                values.append({'es': variant.option3_value})
            
            # SIEMPRE agregar values (requerido por API, incluso si está vacío)
            v["values"] = values
            if values:
                _logger.info("   ✅ values agregados: %s", values)
            else:
                _logger.info("   ✅ values agregado: [] (vacío, requerido por API)")

            # Stock opcional: si querés manejar stock por API de variantes, se puede incluir aquí.
            # La doc permite 'stock', pero 'stock_management' es automático.
            # Stock solo si sync_stock es True
            if sync_stock:
                _logger.info("   📦 PROCESANDO STOCK:")
                _logger.info("      - Stock en variante de publicación: %s (tipo: %s)", 
                           variant.stock, type(variant.stock).__name__)
                
                if variant.stock is not None:
                    try:
                        # Convertir a entero (TiendaNube solo acepta enteros en stock)
                        stock_int = int(variant.stock)
                        v["stock"] = stock_int
                        _logger.info("      ✅ stock convertido y agregado al payload: %d (int)", stock_int)
                        _logger.info("      📋 Estructura del campo stock en payload: {'stock': %d}", stock_int)
                    except (ValueError, TypeError) as e:
                        _logger.warning("      ⚠️ Error convirtiendo stock a entero: %s (valor: %s)", e, variant.stock)
                        _logger.warning("      ⚠️ Stock NO se agregará al payload")
                else:
                    _logger.info("      ℹ️ Stock es None, no se agregará al payload")

            _logger.info("   📦 Variante preparada (payload completo):")
            _logger.info("      %s", json.dumps(v, indent=6, ensure_ascii=False))
            variants_payload.append(v)

        if not variants_payload:
            _logger.warning("⚠️ No hay variantes con tn_variant_id para actualizar en TiendaNube")
            return

        _logger.info("=" * 100)
        _logger.info("📦 RESUMEN: Payload PUT /products/%s/variants (%d variantes)", product_id, len(variants_payload))
        _logger.info("=" * 100)
        for idx, v_payload in enumerate(variants_payload):
            _logger.info("   Variante %d:", idx)
            _logger.info("      - ID TiendaNube: %s", v_payload.get('id'))
            _logger.info("      - Precio: %s", v_payload.get('price'))
            _logger.info("      - Stock: %s", v_payload.get('stock', 'NO INCLUIDO'))
            _logger.info("      - SKU: %s", v_payload.get('sku', 'NO INCLUIDO'))
            _logger.debug("      - Payload completo: %s", json.dumps(v_payload, indent=2, ensure_ascii=False))
        try:
            _logger.debug("📤 JSON Payload (PUT variantes): %s", json.dumps(variants_payload, indent=2, ensure_ascii=False))
        except Exception as e:
            _logger.warning("⚠️ No se pudo serializar JSON para log: %s", e)
        try:
            endpoint = f"/products/{product_id}/variants"
            _logger.info("=" * 100)
            _logger.info("📤 ENVIANDO PUT A TIENDANUBE")
            _logger.info("   Endpoint: %s", endpoint)
            _logger.info("   Cantidad de variantes: %d", len(variants_payload))
            _logger.info("=" * 100)
            
            # El JSON completo también se mostrará en _make_request
            start_put = time.time()
            try:
                response = self._make_request('PUT', endpoint, data=variants_payload, retry=False, tn_config=tn_config)
            except Exception as e:
                duration_ms = int((time.time() - start_put) * 1000)
                self._tn_audit_sync(
                    publication,
                    _tn_op_label(),
                    'error',
                    tn_config=tn_config,
                    value_before='-',
                    value_after=_tn_fmt_bulk_audit(variants_payload),
                    http_status=0,
                    duration_ms=duration_ms,
                    error_message=(str(e) or '')[:65535],
                    force_log=True,
                )
                raise
            duration_ms = int((time.time() - start_put) * 1000)
            self._tn_audit_sync(
                publication,
                _tn_op_label(),
                'ok',
                tn_config=tn_config,
                value_before='-',
                value_after=_tn_fmt_bulk_audit(variants_payload),
                http_status=200,
                duration_ms=duration_ms,
                force_log=True,
            )
            
            _logger.info("=" * 100)
            _logger.info("✅ RESPUESTA DE TIENDANUBE")
            _logger.info("   Product ID: %s", product_id)
            _logger.info("   Variantes actualizadas: %d", len(variants_payload))
            if response:
                try:
                    _logger.debug("   Respuesta completa: %s", json.dumps(response, indent=4, ensure_ascii=False))
                except Exception:
                    _logger.debug("   Respuesta: %s", str(response))
            else:
                _logger.warning("   ⚠️ Respuesta vacía o None")
            _logger.info("=" * 100)
        except Exception as e:
            _logger.error("=" * 100)
            _logger.error("❌ ERROR ACTUALIZANDO VARIANTES EN TIENDANUBE")
            _logger.error("   Product ID: %s", product_id)
            _logger.error("   Error: %s", str(e))
            _logger.error("=" * 100)
            _logger.exception("Detalles completos del error:")
            raise

    def _refresh_virtual_variant_id(self, publication, product_id):
        """
        Obtiene la variante virtual de TiendaNube cuando no hay variantes definidas en Odoo.
        Crea un registro temporal de variante virtual para poder actualizarla.
        """
        try:
            _logger.info("🔄 Obteniendo variante virtual desde API para producto %s", product_id)
            response = self._make_request('GET', f'/products/{product_id}', retry=False)
            if not response or not isinstance(response, dict):
                _logger.warning("⚠️ No se obtuvo respuesta válida de la API")
                return

            api_variants = response.get('variants') or []
            if not api_variants:
                _logger.warning("⚠️ No hay variantes en TiendaNube para producto %s", product_id)
                return

            # Si solo hay una variante, es la variante virtual
            if len(api_variants) == 1:
                virtual_variant_id = api_variants[0].get('id')
                _logger.info("✅ Variante virtual encontrada: ID %s", virtual_variant_id)
                
                # Crear una variante temporal en Odoo para poder actualizarla
                # Usar el modelo directamente para crear un registro temporal
                VariantModel = self.env['tn.publication.variant']
                virtual_variant = VariantModel.create({
                    'publication_id': publication.id,
                    'name': 'Variante Virtual',
                    'tn_variant_id': virtual_variant_id,
                    'price': float(api_variants[0].get('price', 0)),
                    'stock': api_variants[0].get('stock', 0),
                    'sku': publication.sku or '',
                    'weight': publication.weight or 0.0,
                    'width': publication.width or 0.0,
                    'height': publication.height or 0.0,
                    'depth': publication.depth or 0.0,
                    'barcode': publication.barcode or '',
                })
                _logger.info("✅ Variante virtual temporal creada en Odoo con ID %s", virtual_variant.id)
            else:
                _logger.warning("⚠️ Hay %d variantes en TiendaNube, no es una variante virtual única", len(api_variants))
        except Exception as e:
            _logger.error("❌ Error obteniendo variante virtual desde API: %s", e, exc_info=True)

    def _refresh_variant_ids_from_api(self, publication, product_id, tn_config=None):
        """
        Obtiene las variantes desde la API y mapea tn_variant_id por SKU o por posición.
        Esto permite luego actualizar precio/stock vía PUT /variants/{id}.
        Si la publicación ya fue exportada a TN pero las variantes en Odoo no tienen
        tn_variant_id, este método los rellena para que el sync stock/precio funcione.
        """
        try:
            _logger.info("🔄 Obteniendo variantes desde API para producto %s", product_id)
            response = self._make_request('GET', f'/products/{product_id}', retry=False, tn_config=tn_config)
            if not response or not isinstance(response, dict):
                _logger.warning("⚠️ No se obtuvo respuesta válida de la API")
                return

            api_variants = response.get('variants') or []
            if not api_variants:
                _logger.warning("⚠️ No hay variantes en TiendaNube para producto %s", product_id)
                return

            _logger.info("🔍 Variantes en TiendaNube: %d", len(api_variants))
            for idx, v in enumerate(api_variants):
                _logger.info("   - Variante %d: id=%s, sku='%s', position=%s", 
                            idx, v.get('id'), v.get('sku'), v.get('position'))

            # Crear un índice por SKU (lower) para emparejar
            sku_to_id = {}
            for v in api_variants:
                sku_v = (v.get('sku') or '').strip().lower()
                if sku_v:
                    sku_to_id[sku_v] = v.get('id')
                    _logger.debug("   Mapeado SKU '%s' -> ID %s", sku_v, v.get('id'))

            # Mapear variantes locales a variantes de TiendaNube
            local_variants = list(publication.variant_ids)
            _logger.info("🔍 Variantes locales en Odoo: %d", len(local_variants))
            for idx, variant in enumerate(local_variants):
                _logger.info("   - Variante local %d: nombre='%s', sku='%s', tn_variant_id=%s", 
                            idx, variant.name or 'sin nombre', variant.sku, variant.tn_variant_id)
                
                # Si ya tiene ID, continuar
                if variant.tn_variant_id:
                    _logger.info("     ✅ Ya tiene tn_variant_id=%s", variant.tn_variant_id)
                    continue
                
                # Intentar mapear por SKU primero
                sku_local = (variant.sku or '').strip().lower()
                if sku_local and sku_local in sku_to_id:
                    variant.tn_variant_id = sku_to_id[sku_local]
                    _logger.info("     ✅ Mapeado por SKU '%s' -> ID %s", sku_local, sku_to_id[sku_local])
                    continue
                
                # Si no hay SKU o no coincide, mapear por posición (solo si hay igual cantidad de variantes)
                if len(local_variants) == len(api_variants):
                    # Ordenar variantes de TiendaNube por position
                    sorted_api_variants = sorted(api_variants, key=lambda x: x.get('position', 999))
                    if idx < len(sorted_api_variants):
                        api_variant_id = sorted_api_variants[idx].get('id')
                        variant.tn_variant_id = api_variant_id
                        _logger.info("     ✅ Mapeado por posición %d -> ID %s", idx, api_variant_id)
                        continue
                
                # Si solo hay una variante en ambos lados, mapear directamente
                if len(local_variants) == 1 and len(api_variants) == 1:
                    api_variant_id = api_variants[0].get('id')
                    variant.tn_variant_id = api_variant_id
                    _logger.info("     ✅ Mapeado único (solo hay una variante) -> ID %s", api_variant_id)
                    continue
                
                _logger.warning("     ⚠️ No se pudo mapear variante '%s' (sku='%s')", 
                               variant.name or 'sin nombre', variant.sku)
        except Exception as e:
            _logger.error("❌ Error obteniendo variantes desde API para mapear IDs: %s", e, exc_info=True)

    def _export_images(self, publication, product_id):
        """Exporta imágenes a TiendaNube. Soporta attachment (base64) o src (url)."""
        # Obtener imágenes de la publicación
        image_records = publication.image_ids if hasattr(publication, 'image_ids') else []
        
        # Obtener imágenes existentes en TiendaNube para sincronizar
        try:
            existing_images_response = self._make_request('GET', f'/products/{product_id}/images', retry=False)
            existing_images = existing_images_response if isinstance(existing_images_response, list) else []
            existing_image_ids = {img.get('id') for img in existing_images if img.get('id')}
            _logger.info("📷 Imágenes existentes en TiendaNube: %d (IDs: %s)", len(existing_images), existing_image_ids)
        except Exception as e:
            _logger.warning("⚠️ No se pudieron obtener imágenes existentes de TiendaNube: %s", str(e))
            existing_images = []
            existing_image_ids = set()
        
        # Obtener IDs de imágenes en Odoo que ya están en TiendaNube
        odoo_image_ids = {img.tn_image_id for img in image_records if img.tn_image_id}
        
        # Eliminar imágenes que están en TiendaNube pero no en Odoo
        # Solo eliminar si hay al menos una imagen en Odoo con tn_image_id (para evitar eliminar todo si no hay IDs)
        # O si no hay imágenes en Odoo (caso: usuario eliminó todas las imágenes)
        images_to_delete = existing_image_ids - odoo_image_ids
        
        # Si hay imágenes para eliminar y (hay imágenes en Odoo con IDs O no hay imágenes en Odoo)
        should_delete = images_to_delete and (odoo_image_ids or not image_records)
        
        if should_delete:
            _logger.info("🗑️ Eliminando %d imágenes de TiendaNube que ya no están en Odoo (IDs: %s)", 
                        len(images_to_delete), images_to_delete)
            for img_id in images_to_delete:
                try:
                    self.delete_image_from_tn(product_id, img_id)
                    _logger.info("✅ Imagen %s eliminada exitosamente de TiendaNube", img_id)
                    time.sleep(0.5)  # Pequeña pausa entre eliminaciones
                except Exception as e:
                    _logger.warning("⚠️ Error al eliminar imagen %s de TiendaNube: %s", img_id, str(e))
        elif images_to_delete and not odoo_image_ids and image_records:
            # Hay imágenes en Odoo pero ninguna tiene tn_image_id - no eliminar por seguridad
            _logger.warning("⚠️ Hay %d imágenes en TiendaNube pero ninguna imagen en Odoo tiene tn_image_id. "
                          "No se eliminarán automáticamente para evitar pérdida de datos. "
                          "Sincroniza primero las imágenes o elimínalas manualmente.", len(existing_image_ids))
        elif existing_image_ids and not images_to_delete:
            _logger.info("✅ Todas las imágenes en TiendaNube están sincronizadas con Odoo")
        
        # Si no hay imágenes en Odoo, solo eliminar las de TiendaNube (ya hecho arriba) y salir
        if not image_records:
            _logger.info("📷 No hay imágenes en publication.image_ids para exportar")
            return
        
        _logger.info("📷 Encontradas %d imágenes para exportar a TiendaNube (product_id: %s)", len(image_records), product_id)
        
        for image in image_records.sorted('position'):
            # Si la imagen ya tiene tn_image_id y existe en TiendaNube, omitir (ya está sincronizada)
            if image.tn_image_id and image.tn_image_id in existing_image_ids:
                _logger.debug("⏭️ Imagen %s ya existe en TiendaNube (ID: %s), omitiendo", image.name or 'sin nombre', image.tn_image_id)
                continue
            
            # Preferir URL si está disponible (más confiable que base64)
            image_url = getattr(image, 'image_url', None)
            image_binary = getattr(image, 'image', None)
            
            if not image_binary and not image_url:
                _logger.warning("⚠️ Imagen sin contenido (sin image ni image_url), omitiendo")
                continue

            try:
                payload = {}
                decoded_bytes = None
                mime_type = None
                payload_decoded_bytes = None
                payload_mime_type = None
                
                # Preferir URL si está disponible (evita problemas con base64 corrupto)
                if image_url and image_url.startswith(('http://', 'https://')):
                    payload = {
                        "src": image_url
                    }
                    payload_decoded_bytes = None
                    payload_mime_type = None
                    _logger.info("📤 Subiendo imagen desde URL: %s", image_url)
                # Si image.image parece base64 (binary in odoo), usar attachment
                elif image_binary:
                    # En Odoo los binaries suelen venir como base64 string
                    attachment_raw = image.image
                    
                    # Procesar el base64: decodificar y recodificar para asegurar formato correcto
                    try:
                        import base64 as b64
                        
                        if isinstance(attachment_raw, str):
                            # Remover prefijo data:image/...;base64, si existe
                            if ',' in attachment_raw:
                                attachment_raw = attachment_raw.split(',', 1)[1]
                            # Limpiar espacios y saltos de línea
                            attachment_raw = attachment_raw.strip()
                            
                            # Decodificar base64 UNA SOLA VEZ - nunca intentar doble decodificación
                            try:
                                attachment_clean = attachment_raw.strip()
                                
                                # Decodificar una vez
                                try:
                                    decoded_bytes = b64.b64decode(attachment_clean, validate=True)
                                except Exception:
                                    # Si falla estricto, intentar laxo
                                    decoded_bytes = b64.b64decode(attachment_clean, validate=False)

                                if not decoded_bytes:
                                    _logger.error("❌ No se pudo decodificar la imagen base64.")
                                    continue

                                # Validar magic; si no es imagen conocida, verificar si es base64 doblemente codificado
                                magic = decoded_bytes[:12]
                                is_valid_image = (magic.startswith(b'\xff\xd8\xff') or 
                                                magic.startswith(b'\x89PNG') or 
                                                magic.startswith(b'GIF') or 
                                                (magic.startswith(b'RIFF') and b'WEBP' in magic[:12]))
                                
                                # Si no es imagen válida, verificar si el contenido decodificado es base64 textual
                                if not is_valid_image:
                                    try:
                                        # Intentar decodificar como texto ASCII para ver si es base64
                                        decoded_text = decoded_bytes.decode('ascii', errors='ignore')
                                        # Si parece base64 (solo caracteres base64 válidos y longitud múltiplo de 4)
                                        if len(decoded_text) > 20 and re.match(r'^[A-Za-z0-9+/=\s]+$', decoded_text) and len(decoded_text.strip()) % 4 == 0:
                                            # Intentar segunda decodificación
                                            _logger.debug("🔁 Contenido parece base64 doblemente codificado, decodificando nuevamente...")
                                            decoded_second = b64.b64decode(decoded_text.strip(), validate=True)
                                            # Validar magic de la segunda decodificación
                                            magic_second = decoded_second[:12]
                                            is_valid_second = (magic_second.startswith(b'\xff\xd8\xff') or 
                                                              magic_second.startswith(b'\x89PNG') or 
                                                              magic_second.startswith(b'GIF') or 
                                                              (magic_second.startswith(b'RIFF') and b'WEBP' in magic_second[:12]))
                                            if is_valid_second:
                                                _logger.debug("✅ Segunda decodificación exitosa (magic=%s)", magic_second)
                                                decoded_bytes = decoded_second
                                                magic = magic_second
                                                is_valid_image = True
                                    except Exception:
                                        pass
                                
                                # Si después de todo no es imagen válida, omitir
                                if not is_valid_image:
                                    _logger.error("❌ El contenido decodificado no parece imagen válida (magic=%s). Omitiendo.", magic)
                                    continue

                                # Re-encode limpio para enviar como JSON
                                attachment_b64 = b64.b64encode(decoded_bytes).decode('utf-8')
                                
                                # Validar que el base64 re-encodado sea válido (puede decodificarse correctamente)
                                try:
                                    test_decode = b64.b64decode(attachment_b64, validate=True)
                                    if test_decode != decoded_bytes:
                                        _logger.error("❌ Base64 re-encodado no coincide con original. Omitiendo.")
                                        continue
                                    _logger.debug("✅ Base64 re-encodado válido (tamaño: %d bytes)", len(decoded_bytes))
                                except Exception as validate_error:
                                    _logger.error("❌ Base64 re-encodado inválido: %s", str(validate_error))
                                    continue

                            except Exception as decode_error:
                                _logger.error("❌ Error decodificando base64: %s", str(decode_error))
                                continue
                                
                        elif isinstance(attachment_raw, bytes):
                            # Si ya es bytes, codificar a base64 y conservar binario
                            decoded_bytes = attachment_raw
                            attachment_b64 = b64.b64encode(attachment_raw).decode('utf-8')
                        else:
                            _logger.warning("⚠️ Formato de imagen no reconocido: %s", type(attachment_raw))
                            continue
                            
                    except Exception as e:
                        _logger.error("❌ Error procesando imagen base64: %s", str(e))
                        continue
                    
                    # Verificar que no esté vacío y tenga tamaño mínimo razonable
                    if not attachment_b64 or len(attachment_b64) < 100:
                        _logger.warning("⚠️ Imagen base64 muy pequeña o vacía (%d chars), omitiendo", len(attachment_b64) if attachment_b64 else 0)
                        continue
                    
                    # Determinar extensión del archivo - asegurarse de que siempre tenga un nombre válido
                    filename = (image.name or '').strip()
                    
                    # Detectar tipo de imagen desde el contenido decodificado
                    try:
                        decoded_sample = base64.b64decode(attachment_b64[:200])
                        if decoded_sample.startswith(b'\xff\xd8\xff'):
                            ext = '.jpg'
                        elif decoded_sample.startswith(b'\x89PNG'):
                            ext = '.png'
                        elif decoded_sample.startswith(b'GIF'):
                            ext = '.gif'
                        elif decoded_sample.startswith(b'RIFF') and b'WEBP' in decoded_sample[:20]:
                            ext = '.webp'
                        else:
                            ext = '.jpg'  # Default
                    except:
                        ext = '.jpg'  # Default si falla la detección
                    
                    if not filename:
                        # Si no hay nombre, generar uno basado en la posición y tipo detectado
                        filename = f'image_{image.position or 1}{ext}'
                    else:
                        # Limpiar filename de caracteres inválidos pero mantener extensión si es válida
                        # Remover extensión temporalmente para limpiar
                        base_name = re.sub(r'\.[^.]+$', '', filename) if '.' in filename else filename
                        base_name = re.sub(r'[^\w\-_]', '_', base_name)
                        # Si no tiene extensión o la extensión no es válida, usar la detectada
                        if not filename.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                            filename = base_name + ext if base_name else f'image_{image.position or 1}{ext}'
                        else:
                            filename = base_name + os.path.splitext(filename)[1] if base_name else f'image_{image.position or 1}{ext}'
                    
                    # Asegurarse de que el filename no esté vacío
                    if not filename or filename == ext:
                        filename = f'image_{image.position or 1}{ext}'
                    
                    # Validar filename: detectar filenames inválidos (vacíos, solo guiones/guiones bajos, sin caracteres alfanuméricos)
                    # Extraer nombre base y extensión para validar
                    filename_base = os.path.splitext(filename)[0] if '.' in filename else filename
                    filename_ext = os.path.splitext(filename)[1] if '.' in filename else ''
                    
                    # Validar que el nombre base tenga al menos un carácter alfanumérico
                    has_alphanumeric = bool(re.search(r'[a-zA-Z0-9]', filename_base))
                    # Validar que no sea solo guiones o guiones bajos
                    is_only_separators = bool(re.match(r'^[_\-\s]+$', filename_base))
                    
                    # Si el filename es inválido, reemplazarlo
                    if not filename_base or is_only_separators or not has_alphanumeric:
                        # Conservar extensión si es válida, usar la detectada si no
                        valid_ext = filename_ext if filename_ext.lower() in ('.jpg', '.jpeg', '.png', '.gif', '.webp') else ext
                        # Generar nombre seguro: image_<posición> o uuid corto si no hay posición
                        if image.position:
                            filename = f'image_{image.position}{valid_ext}'
                        else:
                            # Usar uuid corto como fallback
                            import uuid
                            short_uuid = str(uuid.uuid4())[:8]
                            filename = f'image_{short_uuid}{valid_ext}'
                        _logger.warning("⚠️ Filename inválido detectado, reemplazado por: %s", filename)
                    
                    # Formato correcto según TiendaNube: filename y attachment en nivel superior (SIN wrapper "image")
                    payload = {
                        "filename": filename,
                        "attachment": attachment_b64
                    }
                    # Guardar binario y mime_type - decoded_bytes ya está validado (puede haber sido decodificado dos veces)
                    # Asegurarse de que decoded_bytes sea el binario final válido
                    payload_decoded_bytes = decoded_bytes  # Ya validado arriba, puede ser resultado de segunda decodificación
                    payload_mime_type = mime_type or 'application/octet-stream'
                    
                    # Verificación final: asegurarse de que payload_decoded_bytes tenga magic válido
                    if payload_decoded_bytes:
                        final_magic = payload_decoded_bytes[:12]
                        is_final_valid = (final_magic.startswith(b'\xff\xd8\xff') or 
                                         final_magic.startswith(b'\x89PNG') or 
                                         final_magic.startswith(b'GIF') or 
                                         (final_magic.startswith(b'RIFF') and b'WEBP' in final_magic[:12]))
                        if not is_final_valid:
                            _logger.error("❌ payload_decoded_bytes no tiene magic válido (magic=%s). Esto indica un error en el flujo de decodificación.", final_magic)
                            # Intentar una última decodificación si parece base64
                            try:
                                decoded_text = payload_decoded_bytes.decode('ascii', errors='ignore')
                                if len(decoded_text) > 20 and re.match(r'^[A-Za-z0-9+/=\s]+$', decoded_text) and len(decoded_text.strip()) % 4 == 0:
                                    _logger.warning("⚠️ payload_decoded_bytes todavía es base64, decodificando una vez más...")
                                    decoded_final = b64.b64decode(decoded_text.strip(), validate=True)
                                    final_magic_check = decoded_final[:12]
                                    is_final_check_valid = (final_magic_check.startswith(b'\xff\xd8\xff') or 
                                                           final_magic_check.startswith(b'\x89PNG') or 
                                                           final_magic_check.startswith(b'GIF') or 
                                                           (final_magic_check.startswith(b'RIFF') and b'WEBP' in final_magic_check[:12]))
                                    if is_final_check_valid:
                                        _logger.info("✅ Decodificación final exitosa (magic=%s)", final_magic_check)
                                        payload_decoded_bytes = decoded_final
                                        decoded_bytes = decoded_final
                                        # Re-encodear attachment_b64 con el binario correcto
                                        attachment_b64 = b64.b64encode(decoded_final).decode('utf-8')
                                        payload['attachment'] = attachment_b64
                                    else:
                                        _logger.error("❌ Después de tercera decodificación, magic sigue inválido (magic=%s). Omitiendo.", final_magic_check)
                                        continue
                            except Exception as final_decode_error:
                                _logger.error("❌ Error en decodificación final: %s", str(final_decode_error))
                                continue
                    _logger.info("📤 Subiendo imagen (base64, %d chars, filename: %s)", len(attachment_b64), filename)
                    _logger.debug("📤 Payload imagen (primeros 100 chars de base64): %s...", attachment_b64[:100])
                    
                    # Validaciones adicionales del base64 antes de enviar
                    try:
                        import base64 as b64
                        # Verificar que el base64 completo sea válido
                        try:
                            # Decodificar el base64 completo para validar
                            decoded_full = decoded_bytes if decoded_bytes else b64.b64decode(attachment_b64, validate=True)
                            # Si aún parece texto base64, intentar una segunda decodificación para obtener bytes reales
                            try:
                                def _looks_like_b64(data_bytes):
                                    try:
                                        txt = data_bytes.decode('ascii')
                                    except Exception:
                                        return False
                                    return re.match(r'^[A-Za-z0-9+/=\s]+$', txt) and len(txt.strip()) % 4 == 0
                                if _looks_like_b64(decoded_full):
                                    decoded_second = b64.b64decode(decoded_full, validate=False)
                                    if decoded_second:
                                        _logger.debug("🔁 Segunda decodificación aplicada sobre buffer todavía base64.")
                                        decoded_full = decoded_second
                                        decoded_bytes = decoded_second
                                        attachment_b64 = b64.b64encode(decoded_second).decode('utf-8')
                            except Exception:
                                pass
                            # Verificar tamaño (TiendaNube: <10MB según docs)
                            size_mb = len(decoded_full) / (1024 * 1024)
                            if size_mb > 10:
                                _logger.warning("⚠️ Imagen muy grande (%.2f MB > 10MB permitido por TiendaNube). Omitiendo.", size_mb)
                                continue
                            # Descartar imágenes prácticamente vacías (<5KB)
                            if size_mb < 0.005:
                                _logger.warning("⚠️ Imagen demasiado pequeña (%.4f MB), se omite por ser vacía.", size_mb)
                                continue

                            # Validar magic numbers básicos para formatos soportados; si no, omitir
                            magic = decoded_full[:12]
                            is_jpg = magic.startswith(b'\xff\xd8\xff')
                            is_png = magic.startswith(b'\x89PNG')
                            is_gif = magic.startswith(b'GIF')
                            is_webp = magic.startswith(b'RIFF') and b'WEBP' in magic[:12]
                            if not any([is_jpg, is_png, is_gif, is_webp]):
                                _logger.error("❌ Formato no soportado (magic=%s). Omitiendo imagen.", magic)
                                continue
                            # Derivar mime_type
                            mime_type = 'image/jpeg' if is_jpg else 'image/png' if is_png else 'image/gif' if is_gif else 'image/webp'

                            _logger.debug("✅ Base64 válido (tamaño: %.2f MB, primeros bytes: %s)", size_mb, decoded_full[:10])
                        except Exception as decode_error:
                            _logger.error("❌ Base64 inválido o corrupto: %s", str(decode_error))
                            continue
                        
                        # Verificar que el base64 no tenga caracteres inválidos (solo A-Z, a-z, 0-9, +, /, =)
                        if not re.match(r'^[A-Za-z0-9+/=]+$', attachment_b64):
                            _logger.error("❌ Base64 contiene caracteres inválidos")
                            continue
                            
                    except Exception as e:
                        _logger.error("❌ Error validando base64: %s", str(e))
                        continue
                
                # Si llegamos aquí sin payload, algo salió mal
                if not payload:
                    _logger.warning("⚠️ No se pudo preparar payload para imagen. Omitiendo.")
                    continue

                # Validación final del payload antes de enviar
                if 'attachment' in payload:
                    # Verificar que attachment no esté vacío
                    if not payload['attachment'] or len(payload['attachment']) < 100:
                        _logger.error("❌ Attachment base64 vacío o muy pequeño, omitiendo")
                        continue
                    # Verificar que filename sea válido
                    if not payload.get('filename') or len(payload['filename']) < 3:
                        _logger.error("❌ Filename inválido: %s", payload.get('filename'))
                        continue
                
                # TiendaNube solo acepta JSON (no multipart), usar siempre JSON
                if 'attachment' in payload and payload_decoded_bytes:
                    size_kb = len(payload_decoded_bytes) / 1024
                    _logger.info("📤 Imagen (%.2f KB), usando JSON", size_kb)
                
                _logger.info("📤 Endpoint: POST /products/%s/images", product_id)
                _logger.info("📤 Payload JSON: %s", {k: (v[:50] + '...' if isinstance(v, str) and len(v) > 50 else v) for k, v in payload.items()})
                
                # Validación final: verificar que el base64 del payload decodifique correctamente
                if 'attachment' in payload and payload_decoded_bytes:
                    # Verificar que payload_decoded_bytes tenga magic válido (ya validado arriba)
                    test_magic = payload_decoded_bytes[:12]
                    is_valid_image = (test_magic.startswith(b'\xff\xd8\xff') or 
                                    test_magic.startswith(b'\x89PNG') or 
                                    test_magic.startswith(b'GIF') or 
                                    (test_magic.startswith(b'RIFF') and b'WEBP' in test_magic[:12]))
                    if not is_valid_image:
                        _logger.error("❌ Base64 final no contiene imagen válida (magic=%s). Omitiendo.", test_magic)
                        continue
                    # Verificar que el base64 del payload decodifique al mismo binario
                    try:
                        test_decode = base64.b64decode(payload['attachment'], validate=True)
                        if test_decode != payload_decoded_bytes:
                            _logger.error("❌ Base64 del payload no coincide con binario validado. Omitiendo.")
                            continue
                        _logger.debug("✅ Base64 final validado correctamente (magic=%s, size=%d bytes)", test_magic, len(payload_decoded_bytes))
                    except Exception as final_validate_error:
                        _logger.error("❌ Base64 del payload no puede decodificarse: %s", str(final_validate_error))
                        continue
                
                # Reintentos automáticos para errores 500 (rate limiting de TiendaNube)
                max_attempts = 3
                response = None
                for attempt in range(max_attempts):
                    try:
                        # TiendaNube solo acepta JSON para imágenes (tamaño no importa)
                        response = self._make_request(
                            'POST',
                            f'/products/{product_id}/images',
                            data=payload,  # payload JSON completo con filename y attachment
                            retry=False  # Desactivar retry global, manejamos reintentos aquí
                        )
                        _logger.info("✅ Imagen subida exitosamente (intento %d/%d). Respuesta: %s", attempt + 1, max_attempts, response)
                        
                        # Guardar el tn_image_id en la imagen de Odoo
                        if response and isinstance(response, dict) and response.get('id'):
                            image.tn_image_id = response.get('id')
                            _logger.debug("💾 Guardado tn_image_id=%s para imagen %s", response.get('id'), image.name or 'sin nombre')
                        
                        break  # Éxito, salir del loop
                    except UserError as ue:
                        error_msg = str(ue)
                        # Si es error 500 y quedan intentos, reintentar con delay
                        if '500' in error_msg and attempt < max_attempts - 1:
                            wait_time = 1.5 * (attempt + 1)  # Backoff: 1.5s, 3s, 4.5s
                            _logger.warning("⚠️ Error 500 en intento %d/%d. Reintentando en %.1f segundos...", 
                                           attempt + 1, max_attempts, wait_time)
                            time.sleep(wait_time)
                            continue
                        # Si no es 500 o se agotaron los intentos, lanzar error
                        _logger.error("❌ Error al subir imagen (UserError): %s", error_msg)
                        if '500' in error_msg:
                            _logger.warning("⚠️ Error 500 de TiendaNube después de %d intentos. Omitiendo imagen.", max_attempts)
                        # Continuar con la siguiente imagen en lugar de fallar todo
                        break
                    except Exception as e:
                        _logger.error("❌ Error inesperado al subir imagen (intento %d/%d): %s", 
                                    attempt + 1, max_attempts, str(e), exc_info=True)
                        if attempt < max_attempts - 1:
                            time.sleep(1.5)
                            continue
                        # Continuar con la siguiente imagen
                        break

                # Pausa entre imágenes para evitar rate limit (TiendaNube necesita ~1.2s entre imágenes)
                time.sleep(1.2)
            except Exception as e:
                _logger.error("❌ Error general al procesar imagen: %s", str(e), exc_info=True)
                # Continuar con la siguiente imagen

    def _find_category_id_by_name(self, category_name):
        """Busca una categoría en TiendaNube por nombre y retorna su ID (solo búsqueda, sin crear)"""
        try:
            # Buscar todas las categorías
            categories = self._make_request('GET', '/categories', retry=False)
            if not categories or not isinstance(categories, list):
                return None
            
            # Buscar por nombre (puede venir como dict multilenguaje)
            for cat in categories:
                cat_name_field = cat.get('name', '')
                if isinstance(cat_name_field, dict):
                    # Si es multilenguaje, buscar en todos los idiomas
                    for lang, name in cat_name_field.items():
                        if name and name.strip().lower() == category_name.lower():
                            return cat.get('id')
                elif isinstance(cat_name_field, str):
                    if cat_name_field.strip().lower() == category_name.lower():
                        return cat.get('id')
            
            return None
        except Exception as e:
            _logger.warning("⚠️ Error buscando categoría '%s' en TiendaNube: %s", category_name, str(e))
            return None

    def _find_or_create_category_by_name(self, category_name):
        """Busca una categoría en TiendaNube por nombre. Si no existe, la crea y retorna su ID"""
        if not category_name:
            return None
        
        # Primero buscar si existe
        existing_id = self._find_category_id_by_name(category_name)
        if existing_id:
            _logger.debug("✅ Categoría '%s' encontrada en TiendaNube (ID: %s)", category_name, existing_id)
            return existing_id
        
        # Si no existe, crear la categoría
        try:
            _logger.info("📂 Creando categoría '%s' en TiendaNube...", category_name)
            # Generar handle desde el nombre
            handle = self._generate_handle(category_name)
            
            # Crear categoría con nombre multilenguaje
            payload = {
                "name": {"es": category_name},
                "handle": handle,
            }
            
            response = self._make_request('POST', '/categories', data=payload, retry=False)
            if response and isinstance(response, dict):
                new_id = response.get('id')
                if new_id:
                    _logger.info("✅ Categoría '%s' creada exitosamente en TiendaNube (ID: %s)", category_name, new_id)
                    return new_id
                else:
                    _logger.warning("⚠️ Categoría '%s' creada pero no se obtuvo ID en respuesta: %s", category_name, response)
            else:
                _logger.warning("⚠️ Respuesta inesperada al crear categoría '%s': %s", category_name, response)
            return None
        except Exception as e:
            _logger.warning("⚠️ Error creando categoría '%s' en TiendaNube: %s", category_name, str(e))
            return None

    def delete_image_from_tn(self, product_id, image_id):
        """Elimina una imagen de TiendaNube"""
        try:
            endpoint = f'/products/{product_id}/images/{image_id}'
            response = self._make_request('DELETE', endpoint, retry=False)
            _logger.info("✅ Imagen eliminada de TiendaNube: product_id=%s, image_id=%s", product_id, image_id)
            return response
        except Exception as e:
            _logger.error("❌ Error al eliminar imagen de TiendaNube (product_id=%s, image_id=%s): %s", 
                        product_id, image_id, str(e))
            raise

    def _generate_handle(self, title):
        """Genera un handle seguro (string)"""
        if not title:
            return ''
        import re
        handle = title.lower().strip()
        handle = re.sub(r'\s+', '-', handle)
        handle = re.sub(r'[^a-z0-9\-]', '', handle)
        return handle[:100]  # Limitar longitud

    def _clean_html(self, html):
        """Limpia HTML removiendo atributos data-oe-* y espacios extra.
        No agrega dependencias externas."""
        if not html:
            return ''
        import re
        # Eliminar atributos data-oe-* (ej: data-oe-version, data-oe-model)
        cleaned = re.sub(r'\sdata-oe-[a-zA-Z0-9_-]+="[^"]*"', '', html)
        cleaned = re.sub(r"\sdata-oe-[a-zA-Z0-9_-]+='[^']*'", '', cleaned)
        # Opcional: eliminar atributos vacíos estilo data-oe-*
        cleaned = re.sub(r'\sdata-oe-[a-zA-Z0-9_-]+', '', cleaned)
        # Quitar espacios sobrantes
        return cleaned.strip()

    def _clean_html_completely(self, html):
        """Quitar atributos data-oe-* y tags problemáticos. Mantener texto/HTML simple."""
        if not html:
            return ''
        import re
        # Eliminar atributos data-oe-* (ej: data-oe-version)
        cleaned = re.sub(r'\sdata-oe-[a-zA-Z0-9_\-]+=(".*?"|\'.*?\')', '', html)
        cleaned = re.sub(r'\sdata-oe-[a-zA-Z0-9_\-]+', '', cleaned)
        # Opcional: eliminar comments y scripts si existieran
        cleaned = re.sub(r'<!--.*?-->', '', cleaned, flags=re.DOTALL)
        cleaned = re.sub(r'<script.*?>.*?</script>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
        return cleaned.strip()

    def _html_to_text(self, html):
        """Convierte HTML a texto plano"""
        if not html:
            return ''
        import re
        # Remover tags HTML
        text = re.sub(r'<[^>]+>', '', html)
        # Decodificar entidades HTML básicas
        text = text.replace('&nbsp;', ' ')
        text = text.replace('&amp;', '&')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        return text.strip()

    def create_webhook(self, event, callback_url, tn_config=None):
        """
        Crea un webhook en TiendaNube
        
        Args:
            event: Evento a escuchar (ej: 'order/created', 'order/updated')
            callback_url: URL del callback en Odoo (ej: 'https://mi-odoo.com/tiendanube/webhook/order')
            tn_config: Registro de tn.config a usar (opcional)
        
        Returns:
            dict: Respuesta de la API con el ID del webhook creado
        """
        try:
            # Obtener URL base de Odoo
            ICP = self.env['ir.config_parameter'].sudo()
            base_url = ICP.get_param('web.base.url', '')
            if not base_url:
                raise UserError(_('Debe configurar web.base.url en Configuración del Sistema'))

            # Construir URL completa del webhook
            if callback_url.startswith('http'):
                webhook_url = callback_url
            else:
                webhook_url = f"{base_url.rstrip('/')}/{callback_url.lstrip('/')}"

            # Payload según documentación de TiendaNube
            payload = {
                "event": event,
                "url": webhook_url
            }

            if tn_config:
                _logger.info("🔗 Creando webhook en TiendaNube: evento=%s, URL=%s, Store ID=%s", 
                           event, webhook_url, tn_config.store_id)
            else:
                _logger.info("🔗 Creando webhook en TiendaNube: evento=%s, URL=%s", event, webhook_url)

            # Crear webhook usando la configuración específica
            # Endpoint: POST /webhooks según documentación de TiendaNube
            response = self._make_request('POST', '/webhooks', data=payload, retry=False, tn_config=tn_config)

            if response and isinstance(response, dict):
                webhook_id = response.get('id')
                _logger.info("✅ Webhook creado exitosamente en TiendaNube (ID: %s)", webhook_id)
                return {
                    'success': True,
                    'webhook_id': webhook_id,
                    'webhook_url': webhook_url,
                    'response': response
                }
            else:
                _logger.error("❌ Respuesta inesperada al crear webhook: %s", response)
                return {
                    'success': False,
                    'error': 'Respuesta inesperada de TiendaNube'
                }

        except Exception as e:
            _logger.exception("❌ Error creando webhook en TiendaNube: %s", e)
            return {
                'success': False,
                'error': str(e)
            }

    def list_webhooks(self, tn_config=None):
        """Lista todos los webhooks configurados en TiendaNube
        
        Args:
            tn_config: Registro de tn.config a usar (opcional)
        """
        try:
            response = self._make_request('GET', '/webhooks', retry=False, tn_config=tn_config)
            if isinstance(response, list):
                if tn_config:
                    _logger.info("✅ Webhooks listados para Store ID %s: %d encontrados", 
                               tn_config.store_id, len(response))
                else:
                    _logger.info("✅ Webhooks listados: %d encontrados", len(response))
                return response
            return []
        except Exception as e:
            _logger.exception("❌ Error listando webhooks: %s", e)
            return []

    def delete_webhook(self, webhook_id, tn_config=None):
        """Elimina un webhook de TiendaNube"""
        try:
            endpoint = f'/webhooks/{webhook_id}'
            self._make_request('DELETE', endpoint, retry=False, tn_config=tn_config)
            _logger.info("✅ Webhook eliminado: ID=%s", webhook_id)
            return True
        except Exception as e:
            _logger.exception("❌ Error eliminando webhook: %s", e)
            return False

    def search_orders_by_date(self, date_from=None, date_to=None, tn_config=None, limit=100):
        """
        Busca órdenes desde/hasta fechas usando la API de TiendaNube.

        Args:
            date_from: Fecha desde la cual buscar órdenes (datetime o string ISO).
                       Si es None, busca desde hace 7 días.
            date_to: Fecha hasta la cual buscar órdenes (datetime o string ISO). Opcional.
            tn_config: Registro de tn.config a usar (opcional)
            limit: Límite de órdenes por página (máx 200 para /orders)

        Returns:
            list: Lista de órdenes encontradas (dicts)
        """
        try:
            from datetime import datetime, timedelta, date as date_type

            if date_from is None:
                date_from = datetime.now() - timedelta(days=7)

            if isinstance(date_from, datetime):
                date_from_str = date_from.strftime('%Y-%m-%dT%H:%M:%S')
            elif isinstance(date_from, date_type):
                date_from_str = date_from.strftime('%Y-%m-%dT00:00:00')
            else:
                date_from_str = str(date_from)

            date_to_dt = None
            if date_to is not None:
                if isinstance(date_to, datetime):
                    date_to_dt = date_to
                elif isinstance(date_to, date_type):
                    date_to_dt = datetime.combine(date_to, datetime.max.time())
                else:
                    try:
                        date_to_dt = datetime.strptime(str(date_to)[:10], '%Y-%m-%d')
                        date_to_dt = date_to_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
                    except Exception:
                        date_to_dt = None
            
            # Parámetros de búsqueda
            # TiendaNube usa created_at_min para filtrar por fecha de creación
            params = {
                'created_at_min': date_from_str,
                'per_page': min(limit, 200),
                'page': 1
            }
            
            if tn_config:
                _logger.info("🔍 Buscando órdenes desde %s (Store ID: %s)", date_from_str, tn_config.store_id)
            else:
                _logger.info("🔍 Buscando órdenes desde %s (configuración global)", date_from_str)
            page = 1
            has_more = True
            all_orders = []

            while has_more:
                params['page'] = page
                _logger.info("   📄 Consultando página %d...", page)
                
                response = self._make_request('GET', '/orders', params=params, retry=False, tn_config=tn_config)
                
                if not response:
                    _logger.warning("⚠️ Respuesta vacía al buscar órdenes")
                    break
                
                # TiendaNube puede retornar la lista directamente o dentro de un objeto
                orders = []
                if isinstance(response, list):
                    orders = response
                elif isinstance(response, dict):
                    # Buscar en diferentes posibles estructuras
                    orders = response.get('orders', []) or response.get('results', []) or response.get('data', [])
                    if not orders and 'id' in response:
                        # Si la respuesta es una sola orden, convertirla a lista
                        orders = [response]
                
                if not orders:
                    _logger.info("   ℹ️ No hay más órdenes en la página %d", page)
                    has_more = False
                    break
                
                _logger.info("   ✅ Encontradas %d órdenes en la página %d", len(orders), page)
                all_orders.extend(orders)
                
                # Si hay menos órdenes que el límite, no hay más páginas
                if len(orders) < params['per_page']:
                    has_more = False
                else:
                    page += 1
                    # Limitar a 10 páginas para evitar loops infinitos
                    if page > 10:
                        _logger.warning("⚠️ Límite de páginas alcanzado (10). Puede haber más órdenes.")
                        has_more = False

            if date_to_dt is not None and all_orders:
                filtered = []
                for o in all_orders:
                    created = o.get('created_at')
                    if not created:
                        filtered.append(o)
                        continue
                    try:
                        if isinstance(created, str):
                            s = created.replace('Z', '').strip()[:19]
                            created_dt = datetime.strptime(s, '%Y-%m-%dT%H:%M:%S')
                        else:
                            created_dt = created if hasattr(created, 'replace') else datetime.min
                        if created_dt <= date_to_dt:
                            filtered.append(o)
                    except Exception:
                        filtered.append(o)
                all_orders = filtered
                _logger.info("   Filtrado por fecha hasta: %d órdenes", len(all_orders))

            _logger.info("✅ Búsqueda completada: %d órdenes encontradas en total", len(all_orders))
            return all_orders
            
        except Exception as e:
            _logger.exception("❌ Error buscando órdenes por fecha: %s", e)
            return []

    def _get_order_by_id(self, order_id, tn_config=None):
        """
        Obtiene los datos completos de una orden desde la API de TiendaNube
        
        Args:
            order_id: ID de la orden en TiendaNube
            tn_config: Registro de tn.config a usar (opcional)
        
        Returns:
            dict: Datos completos de la orden o None si hay error
        """
        start = time.time()
        try:
            endpoint = f'/orders/{order_id}'
            if tn_config:
                _logger.info("🌐 Obteniendo orden completa: GET %s", endpoint)
                _logger.info("   Configuración: Store ID %s (ID: %s), API URL: %s", 
                           tn_config.store_id, tn_config.id, tn_config.api_base_url or 'default')
            else:
                _logger.info("🌐 Obteniendo orden completa: GET %s (configuración global)", endpoint)
            
            response = self._make_request('GET', endpoint, retry=False, tn_config=tn_config)
            duration_ms = int((time.time() - start) * 1000)
            
            if response and isinstance(response, dict):
                _logger.info("✅ Orden obtenida exitosamente: ID=%s", order_id)
                # Log adicional para debugging: verificar si la respuesta tiene datos válidos
                if 'id' in response:
                    _logger.debug("   Orden tiene ID: %s", response.get('id'))
                else:
                    _logger.warning("⚠️ Respuesta de orden no contiene campo 'id'")
                self._tn_audit_sync(
                    False,
                    'order_import',
                    'ok',
                    tn_config=tn_config,
                    value_before='',
                    value_after=str(order_id),
                    http_status=200,
                    duration_ms=duration_ms,
                    force_log=True,
                )
                return response
            else:
                _logger.error("❌ Respuesta inesperada al obtener orden: %s (tipo: %s)", 
                            response, type(response).__name__)
                if response is None:
                    _logger.error("   La respuesta es None - posible error de autenticación o orden no encontrada")
                elif isinstance(response, dict) and 'error' in response:
                    _logger.error("   Error en respuesta: %s", response.get('error'))
                self._tn_audit_sync(
                    False,
                    'order_import',
                    'error',
                    tn_config=tn_config,
                    value_before='',
                    value_after=str(order_id),
                    http_status=200,
                    duration_ms=duration_ms,
                    error_message='unexpected_response',
                    force_log=True,
                )
                return None
                
        except UserError as e:
            duration_ms = int((time.time() - start) * 1000)
            self._tn_audit_sync(
                False,
                'order_import',
                'error',
                tn_config=tn_config,
                value_before='',
                value_after=str(order_id),
                http_status=0,
                duration_ms=duration_ms,
                error_message=(str(e) or '')[:65535],
                force_log=True,
            )
            # UserError generalmente indica problemas de configuración
            _logger.error("❌ Error de configuración obteniendo orden %s: %s", order_id, str(e))
            raise  # Re-lanzar para que el webhook lo maneje
        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            self._tn_audit_sync(
                False,
                'order_import',
                'error',
                tn_config=tn_config,
                value_before='',
                value_after=str(order_id),
                http_status=0,
                duration_ms=duration_ms,
                error_message=(str(e) or '')[:65535],
                force_log=True,
            )
            _logger.exception("❌ Error obteniendo orden %s desde API: %s", order_id, e)
            return None

    def update_order_fulfillment_status(self, order_id, fulfillment_status):
        """
        Actualiza el estado de fulfillment de una orden en TiendaNube
        
        Args:
            order_id: ID de la orden en TiendaNube
            fulfillment_status: Estado de fulfillment ('to_pack', 'to_ship', 'shipped')
        
        Returns:
            dict: Resultado de la operación con success y response
        """
        try:
            # Mapear estados de Odoo a estados de TiendaNube
            tn_status_map = {
                'to_pack': 'unfulfilled',  # Por empaquetar
                'to_ship': 'fulfilled',    # Empaquetado/Por enviar
                'shipped': 'shipped',      # Enviado
            }
            
            tn_fulfillment_status = tn_status_map.get(fulfillment_status, 'unfulfilled')
            
            endpoint = f'/orders/{order_id}'
            
            # Payload para actualizar el estado de fulfillment
            # Según la documentación de TiendaNube, se puede actualizar el fulfillment_status
            payload = {
                'fulfillment_status': tn_fulfillment_status
            }
            
            _logger.info("🔄 Actualizando estado de fulfillment en TiendaNube")
            _logger.info("   - Endpoint: %s", endpoint)
            _logger.info("   - Orden ID: %s", order_id)
            _logger.info("   - Estado Odoo: %s", fulfillment_status)
            _logger.info("   - Estado TiendaNube: %s", tn_fulfillment_status)
            _logger.info("   - Payload: %s", payload)
            
            # Usar PUT para actualizar la orden
            _logger.info("📡 Enviando solicitud PUT a TiendaNube API...")
            start_put = time.time()
            try:
                response = self._make_request('PUT', endpoint, data=payload, retry=False)
            except Exception as e:
                duration_ms = int((time.time() - start_put) * 1000)
                self._tn_audit_sync(
                    False,
                    'status_change',
                    'error',
                    tn_config=None,
                    value_before=str(fulfillment_status),
                    value_after=tn_fulfillment_status,
                    http_status=0,
                    duration_ms=duration_ms,
                    error_message=(str(e) or '')[:65535],
                    force_log=True,
                )
                raise
            duration_ms = int((time.time() - start_put) * 1000)
            
            _logger.info("📥 Respuesta recibida de TiendaNube API: %s", response)
            
            if response and isinstance(response, dict):
                _logger.info("✅ Estado de fulfillment actualizado exitosamente en TiendaNube")
                _logger.info("   - Orden: %s", order_id)
                _logger.info("   - Nuevo estado: %s", tn_fulfillment_status)
                self._tn_audit_sync(
                    False,
                    'status_change',
                    'ok',
                    tn_config=None,
                    value_before=str(fulfillment_status),
                    value_after=tn_fulfillment_status,
                    http_status=200,
                    duration_ms=duration_ms,
                    force_log=True,
                )
                return {
                    'success': True,
                    'response': response
                }
            else:
                _logger.error("❌ Respuesta inesperada al actualizar fulfillment")
                _logger.error("   - Tipo de respuesta: %s", type(response))
                _logger.error("   - Contenido: %s", response)
                self._tn_audit_sync(
                    False,
                    'status_change',
                    'error',
                    tn_config=None,
                    value_before=str(fulfillment_status),
                    value_after=tn_fulfillment_status,
                    http_status=200,
                    duration_ms=duration_ms,
                    error_message='unexpected_response',
                    force_log=True,
                )
                return {
                    'success': False,
                    'error': 'Respuesta inesperada',
                    'response': response
                }
                
        except Exception as e:
            _logger.exception("❌ Error actualizando fulfillment de orden %s: %s", order_id, e)
            self._tn_audit_sync(
                False,
                'status_change',
                'error',
                tn_config=None,
                value_before=str(fulfillment_status),
                value_after='',
                http_status=0,
                duration_ms=None,
                error_message=(str(e) or '')[:65535],
                force_log=True,
            )
            return {
                'success': False,
                'error': str(e)
            }
    
    def get_order_fulfillment_orders(self, order_id):
        """
        Obtiene todos los fulfillment orders de una orden en TiendaNube.
        
        Args:
            order_id: ID de la orden en TiendaNube
        
        Returns:
            list: Lista de fulfillment orders o None si hay error
        """
        try:
            endpoint = f'/orders/{order_id}/fulfillment-orders'
            
            _logger.info("📦 Obteniendo fulfillment orders de orden %s", order_id)
            _logger.info("   - Endpoint: %s", endpoint)
            
            response = self._make_request('GET', endpoint, retry=False)
            
            if response and isinstance(response, list):
                _logger.info("✅ Se obtuvieron %d fulfillment order(s) para orden %s", len(response), order_id)
                return response
            elif response and isinstance(response, dict):
                # Algunas APIs pueden devolver un objeto con una lista dentro
                fulfillment_orders = response.get('fulfillment_orders', []) or response.get('results', [])
                _logger.info("✅ Se obtuvieron %d fulfillment order(s) para orden %s", len(fulfillment_orders), order_id)
                return fulfillment_orders
            else:
                _logger.warning("⚠️ Respuesta inesperada al obtener fulfillment orders: %s", response)
                return []
                
        except Exception as e:
            _logger.exception("❌ Error obteniendo fulfillment orders de orden %s: %s", order_id, e)
            return []
    
    def update_fulfillment_order_status(self, order_id, fulfillment_order_id, status):
        """
        Actualiza el estado de un fulfillment order en TiendaNube.
        
        Según la documentación: https://tiendanube.github.io/api-documentation/resources/fulfillment-order
        Estados posibles: pending, open, in_progress, ready_to_ship, dispatched, delivered, cancelled
        
        Args:
            order_id: ID de la orden en TiendaNube
            fulfillment_order_id: ID del fulfillment order
            status: Nuevo estado ('delivered', 'dispatched', etc.)
        
        Returns:
            dict: Resultado de la operación con success y response
        """
        try:
            endpoint = f'/orders/{order_id}/fulfillment-orders/{fulfillment_order_id}'
            
            # Payload para actualizar el estado según la documentación
            payload = {
                'status': status
            }
            
            _logger.info("🔄 Actualizando estado de fulfillment order en TiendaNube")
            _logger.info("   - Endpoint: %s", endpoint)
            _logger.info("   - Orden ID: %s", order_id)
            _logger.info("   - Fulfillment Order ID: %s", fulfillment_order_id)
            _logger.info("   - Nuevo estado: %s", status)
            _logger.info("   - Payload: %s", payload)
            
            # Usar PATCH para actualizar el fulfillment order según la documentación
            _logger.info("📡 Enviando solicitud PATCH a TiendaNube API...")
            response = self._make_request('PATCH', endpoint, data=payload, retry=False)
            
            _logger.info("📥 Respuesta recibida de TiendaNube API: %s", response)
            
            if response and isinstance(response, dict):
                _logger.info("✅ Estado de fulfillment order actualizado exitosamente en TiendaNube")
                _logger.info("   - Fulfillment Order: %s", fulfillment_order_id)
                _logger.info("   - Nuevo estado: %s", status)
                return {
                    'success': True,
                    'response': response
                }
            else:
                _logger.error("❌ Respuesta inesperada al actualizar fulfillment order")
                _logger.error("   - Tipo de respuesta: %s", type(response))
                _logger.error("   - Contenido: %s", response)
                return {
                    'success': False,
                    'error': 'Respuesta inesperada',
                    'response': response
                }
                
        except Exception as e:
            _logger.exception("❌ Error actualizando fulfillment order %s de orden %s: %s", 
                            fulfillment_order_id, order_id, e)
            return {
                'success': False,
                'error': str(e)
            }
    
    def mark_fulfillment_order_as_delivered(self, order_id):
        """
        Marca todos los fulfillment orders de una orden como 'delivered' (entregado).
        
        Args:
            order_id: ID de la orden en TiendaNube
        
        Returns:
            dict: Resultado de la operación con success, updated_count y errors
        """
        try:
            _logger.info("=" * 80)
            _logger.info("🚀 INICIO: Marcando fulfillment orders como 'delivered'")
            _logger.info("📦 Orden TiendaNube: %s", order_id)
            
            # Obtener todos los fulfillment orders de la orden
            fulfillment_orders = self.get_order_fulfillment_orders(order_id)
            
            if not fulfillment_orders:
                _logger.warning("⚠️ No se encontraron fulfillment orders para orden %s", order_id)
                _logger.info("=" * 80)
                return {
                    'success': False,
                    'error': 'No se encontraron fulfillment orders',
                    'updated_count': 0
                }
            
            _logger.info("📦 Fulfillment orders encontrados: %d", len(fulfillment_orders))
            
            updated_count = 0
            errors = []
            
            # Actualizar cada fulfillment order a 'delivered'
            for fulfillment_order in fulfillment_orders:
                fulfillment_order_id = fulfillment_order.get('id')
                current_status = fulfillment_order.get('status', 'unknown')
                
                if not fulfillment_order_id:
                    _logger.warning("⚠️ Fulfillment order sin ID, saltando...")
                    continue
                
                _logger.info("📦 Procesando fulfillment order: %s (Estado actual: %s)", 
                           fulfillment_order_id, current_status)
                
                # Solo actualizar si no está ya en 'delivered' o 'cancelled'
                if current_status in ['delivered', 'cancelled']:
                    _logger.info("ℹ️ Fulfillment order %s ya está en estado '%s', no se actualiza", 
                               fulfillment_order_id, current_status)
                    continue
                
                # Actualizar a 'delivered'
                result = self.update_fulfillment_order_status(
                    order_id, 
                    fulfillment_order_id, 
                    'delivered'
                )
                
                if result.get('success'):
                    updated_count += 1
                    _logger.info("✅ Fulfillment order %s actualizado a 'delivered'", fulfillment_order_id)
                else:
                    error_msg = result.get('error', 'Error desconocido')
                    errors.append(f"Fulfillment order {fulfillment_order_id}: {error_msg}")
                    _logger.error("❌ Error actualizando fulfillment order %s: %s", 
                                fulfillment_order_id, error_msg)
            
            _logger.info("✅ Proceso completado: %d fulfillment order(s) actualizado(s)", updated_count)
            if errors:
                _logger.warning("⚠️ Errores encontrados: %s", errors)
            _logger.info("=" * 80)
            
            return {
                'success': updated_count > 0,
                'updated_count': updated_count,
                'total_count': len(fulfillment_orders),
                'errors': errors if errors else None
            }
                
        except Exception as e:
            _logger.exception("❌ Error marcando fulfillment orders como 'delivered' para orden %s: %s", 
                            order_id, e)
            return {
                'success': False,
                'error': str(e),
                'updated_count': 0
            }

