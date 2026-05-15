# -*- coding: utf-8 -*-
import base64
import hashlib
import html
import logging
import secrets
import urllib.parse

import requests
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


def _tn_oauth_state_matches(received, expected):
    """Comparación en tiempo constante; evita error si longitudes difieren."""
    if not received or not expected:
        return False
    a, b = str(received), str(expected)
    try:
        return secrets.compare_digest(a, b)
    except (TypeError, ValueError):
        return False


class TiendaNubeOAuthController(http.Controller):
    """Controlador para manejar el flujo OAuth de TiendaNube"""

    @http.route('/tiendanube/oauth/authorize', type='http', auth='user', website=True)
    def authorize(self, **kwargs):
        """Inicia el flujo OAuth de TiendaNube"""
        try:
            # Obtener configuración (sudo: credenciales con groups= no son legibles para todos los usuarios)
            config = request.env['tn.config'].get_config()
            if not config:
                error_msg = 'No hay configuración de TiendaNube para esta compañía.'
                _logger.error("❌ %s", error_msg)
                return f"""
                <html>
                <head><title>Error de Configuración</title></head>
                <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                    <h1 style="color: red;">❌ Error de Configuración</h1>
                    <p>{html.escape(error_msg)}</p>
                </body>
                </html>
                """
            sec = config.sudo()
            
            if not sec.client_id or not sec.client_secret:
                error_msg = 'Debe configurar Client ID y Client Secret primero en la configuración de TiendaNube'
                _logger.error("❌ %s", error_msg)
                return f"""
                <html>
                <head><title>Error de Configuración</title></head>
                <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                    <h1 style="color: red;">❌ Error de Configuración</h1>
                    <p>{html.escape(error_msg)}</p>
                    <p>Por favor, configura Client ID y Client Secret en la configuración de TiendaNube y vuelve a intentar.</p>
                </body>
                </html>
                """
            
            # PKCE: code_verifier (y challenge si la plataforma lo exige en authorize)
            code_verifier = secrets.token_urlsafe(32)
            code_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode()).digest()
            ).decode('utf-8').rstrip('=')

            # CSRF: state aleatorio que la plataforma debe devolver intacto en el callback (RFC 6749)
            oauth_state = secrets.token_urlsafe(32)

            request.session['tn_code_verifier'] = code_verifier
            request.session['tn_config_id'] = config.id
            request.session['tn_oauth_state'] = oauth_state
            
            # Construir URL de autorización
            # Según la documentación de TiendaNube, la URL correcta es:
            # https://www.tiendanube.com/apps/{app_id}/authorize
            # donde {app_id} es el Client ID
            
            redirect_uri = request.httprequest.host_url.rstrip('/') + '/tiendanube/oauth/callback'
            
            # URL de autorización con client_id en la ruta
            auth_url = f"https://www.tiendanube.com/apps/{sec.client_id}/authorize"
            
            params = {
                'response_type': 'code',
                'redirect_uri': redirect_uri,
                'scope': 'read_products write_products read_orders write_orders',
                'state': oauth_state,
            }

            # PKCE en authorize (descomentar si TiendaNube lo exige en esta URL)
            # params['code_challenge'] = code_challenge
            # params['code_challenge_method'] = 'S256'

            full_url = f"{auth_url}?{urllib.parse.urlencode(params)}"
            
            _logger.info("🔐 Redirigiendo a autorización TiendaNube")
            _logger.info("   URL completa: %s", full_url)
            _logger.info("   Client ID: %s", sec.client_id)
            _logger.info("   Redirect URI: %s", redirect_uri)
            _logger.info("   Host URL: %s", request.httprequest.host_url)
            
            # Verificar que la URL sea válida antes de redirigir
            if not full_url.startswith('http'):
                _logger.error("❌ URL de autorización inválida: %s", full_url)
                return f"""
                <html>
                <head><title>Error</title></head>
                <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                    <h1 style="color: red;">❌ Error</h1>
                    <p>URL de autorización inválida: {html.escape(full_url)}</p>
                    <p>Por favor, verifica la configuración.</p>
                </body>
                </html>
                """
            
            return request.redirect(full_url)
            
        except Exception as e:
            _logger.exception("Error al iniciar autorización OAuth: %s", e)
            return f"""
            <html>
            <head><title>Error</title></head>
            <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                <h1 style="color: red;">❌ Error</h1>
                <p>Error al iniciar autorización: {html.escape(str(e))}</p>
                <p>Por favor, verifica los logs para más detalles.</p>
            </body>
            </html>
            """

    @http.route('/tiendanube/oauth/callback', type='http', auth='public', csrf=False, website=True)
    def callback(self, **kwargs):
        """Callback después de autorizar la aplicación en TiendaNube"""
        code = kwargs.get('code')
        error = kwargs.get('error')
        state_param = kwargs.get('state')
        config_id = request.session.get('tn_config_id')
        code_verifier = request.session.get('tn_code_verifier')
        expected_state = request.session.get('tn_oauth_state')

        def _clear_oauth_session():
            request.session.pop('tn_code_verifier', None)
            request.session.pop('tn_config_id', None)
            request.session.pop('tn_oauth_state', None)

        if not expected_state:
            _logger.warning("Callback OAuth sin state en sesión (posible acceso directo o sesión expirada)")
            _clear_oauth_session()
            return (
                "❌ Sesión de autorización inválida o expirada. "
                "Inicie de nuevo desde Odoo: Configuración TiendaNube → Autorizar aplicación."
            )

        if not state_param or not _tn_oauth_state_matches(state_param, expected_state):
            _logger.warning(
                "Callback OAuth: state inválido o ausente (CSRF o respuesta manipulada)"
            )
            _clear_oauth_session()
            return (
                "❌ Validación de seguridad fallida (parámetro state). "
                "Inicie la autorización de nuevo desde Odoo."
            )

        if error:
            _logger.error("❌ Error en callback OAuth: %s", error)
            _clear_oauth_session()
            return f"❌ Error de autorización: {html.escape(str(error))}"

        if not code:
            _clear_oauth_session()
            return "❌ Falta el parámetro 'code' en la respuesta de TiendaNube."

        if not config_id:
            _clear_oauth_session()
            return "❌ No se encontró la configuración asociada en la sesión."
        
        try:
            # Obtener configuración
            config = request.env['tn.config'].sudo().browse(config_id)
            if not config.exists():
                _clear_oauth_session()
                return f"❌ Configuración TiendaNube no encontrada (ID {html.escape(str(config_id))})."
            
            # Intercambiar code por tokens
            redirect_uri = request.httprequest.host_url.rstrip('/') + '/tiendanube/oauth/callback'
            
            token_url = "https://www.tiendanube.com/apps/authorize/token"
            data = {
                'grant_type': 'authorization_code',
                'client_id': config.client_id,
                'client_secret': config.client_secret,
                'code': code,
                'redirect_uri': redirect_uri,
            }
            
            # Si hay code_verifier (PKCE), agregarlo
            if code_verifier:
                data['code_verifier'] = code_verifier
            
            _logger.info("🔄 Intercambiando code por tokens...")
            response = requests.post(token_url, data=data, timeout=30)
            
            if response.status_code == 200:
                token_data = response.json()
                
                # Guardar tokens
                config.sudo().write({
                    'access_token': token_data.get('access_token', ''),
                    'refresh_token': token_data.get('refresh_token', ''),
                })
                if config.store_id:
                    try:
                        config._sync_store_metadata_from_api()
                    except Exception as sync_e:
                        _logger.warning("OAuth OK pero no se pudo leer país de tienda (GET /store): %s", sync_e)

                _clear_oauth_session()
                
                _logger.info("✅ Tokens obtenidos correctamente")
                
                return """
                <html>
                <head><title>Autorización exitosa</title></head>
                <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                    <h1 style="color: green;">✅ Autorización exitosa</h1>
                    <p>Los tokens de TiendaNube se han guardado correctamente.</p>
                    <p>Puedes cerrar esta ventana y volver a Odoo.</p>
                    <script>
                        setTimeout(function() {
                            window.close();
                        }, 3000);
                    </script>
                </body>
                </html>
                """
            else:
                error_msg = response.text
                _logger.error("❌ Error al obtener tokens: %s - %s", response.status_code, error_msg)
                _clear_oauth_session()
                return f"""
                <html>
                <head><title>Error</title></head>
                <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                    <h1 style="color: red;">❌ Error al obtener tokens</h1>
                    <p>Error {html.escape(str(response.status_code))}: {html.escape(error_msg)}</p>
                </body>
                </html>
                """
                
        except Exception as e:
            _logger.exception("Error en callback OAuth: %s", e)
            _clear_oauth_session()
            return f"""
            <html>
            <head><title>Error</title></head>
            <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                <h1 style="color: red;">❌ Error</h1>
                <p>Error al procesar la autorización: {html.escape(str(e))}</p>
            </body>
            </html>
            """

