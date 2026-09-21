# -*- coding: utf-8 -*-
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
import urllib.parse

import requests
from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)

_TN_AUTHORIZE_BASE = 'https://www.tiendanube.com/apps'
_TN_TOKEN_URL = 'https://www.tiendanube.com/apps/authorize/token'
_INSTALL_TS_TTL = 5 * 60


def _tn_oauth_state_matches(received, expected):
    """Comparación en tiempo constante; evita error si longitudes difieren."""
    if not received or not expected:
        return False
    a, b = str(received), str(expected)
    try:
        return secrets.compare_digest(a, b)
    except (TypeError, ValueError):
        return False


def _oauth_html(title, heading, body, ok=True):
    color = 'green' if ok else 'red'
    return (
        "<html><head><title>%s</title></head>"
        "<body style='font-family:Arial,sans-serif;text-align:center;padding:50px;'>"
        "<h1 style='color:%s;'>%s</h1>"
        "<p>%s</p>"
        "</body></html>"
    ) % (html.escape(title), color, html.escape(heading), body)


class TiendaNubeOAuthController(http.Controller):
    """Controlador para manejar el flujo OAuth de TiendaNube (directo y vía hub)."""

    def _callback_uri(self):
        base = (request.env['ir.config_parameter'].sudo().get_param('web.base.url')
                or request.httprequest.host_url or '').rstrip('/')
        return '%s/tiendanube/oauth/callback' % base

    def _page_error(self, message, title='Error'):
        return _oauth_html(title, title, html.escape(str(message)), ok=False)

    def _exchange_code(self, client_id, client_secret, code, code_verifier=None, redirect_uri=None):
        data = {
            'grant_type': 'authorization_code',
            'client_id': client_id,
            'client_secret': client_secret,
            'code': code,
        }
        if code_verifier:
            data['code_verifier'] = code_verifier
        # Solo el hub manda redirect_uri (debe coincidir con el authorize). El flujo
        # directo histórico no lo envía: TiendaNube ya acepta el intercambio así.
        if redirect_uri:
            data['redirect_uri'] = redirect_uri
        _logger.info('Intercambiando code por tokens...')
        response = requests.post(_TN_TOKEN_URL, data=data, timeout=30)
        return response

    def _apply_tokens(self, config, token_data):
        config.sudo().write({
            'access_token': token_data.get('access_token', ''),
            'refresh_token': token_data.get('refresh_token', ''),
            'store_id': str(token_data.get('user_id') or token_data.get('store_id') or ''),
        })
        try:
            config._sync_store_metadata_from_api()
        except Exception as sync_e:
            _logger.warning('OAuth OK pero GET /store fallo: %s', sync_e)

    def _redirect_to_tiendanube(self, client_id, oauth_state, redirect_uri):
        auth_url = '%s/%s/authorize' % (_TN_AUTHORIZE_BASE, client_id)
        params = {
            'response_type': 'code',
            'redirect_uri': redirect_uri,
            'scope': 'read_products write_products read_orders write_orders',
            'state': oauth_state,
        }
        full_url = '%s?%s' % (auth_url, urllib.parse.urlencode(params))
        _logger.info('Redirigiendo a autorización TiendaNube redirect_uri=%s', redirect_uri)
        return request.redirect(full_url, local=False)

    def _redirect_tenant_to_hub(self, config):
        hub = config._oauth_hub_url()
        secret = config._oauth_hub_secret()
        code = request.env.cr.dbname
        if not hub or not secret:
            return self._page_error(
                'Falta la URL o el secreto del hub OAuth (parámetros de sistema).'
            )
        return_url = request.httprequest.host_url.rstrip('/')
        ts = str(int(time.time()))
        msg = '%s|%s|%s' % (code, return_url, ts)
        sig = hmac.new(secret.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()
        url = '%s/tiendanube/oauth/hub/start?%s' % (
            hub,
            urllib.parse.urlencode({
                'tenant': code,
                'return_url': return_url,
                'ts': ts,
                'sig': sig,
            }),
        )
        _logger.info('OAuth: redirigiendo al hub %s (db=%s)', hub, code)
        return request.redirect(url, local=False)

    def _push_tokens_to_tenant(self, tenant, token_data):
        payload = json.dumps({
            'access_token': token_data.get('access_token') or '',
            'refresh_token': token_data.get('refresh_token') or '',
            'store_id': str(token_data.get('user_id') or token_data.get('store_id') or ''),
            'ts': int(time.time()),
        }, separators=(',', ':'), sort_keys=True)
        sig = tenant._sign_body(payload)
        url = '%s/tiendanube/oauth/install' % tenant._normalized_base_url()
        try:
            response = requests.post(
                url,
                data=payload.encode('utf-8'),
                headers={
                    'Content-Type': 'application/json',
                    'X-TN-OAuth-Signature': sig,
                },
                timeout=30,
            )
        except requests.RequestException as e:
            _logger.exception('Hub OAuth: no se pudo POST a %s', url)
            tenant.sudo().write({'last_error': str(e)[:255]})
            return False, str(e)
        if response.status_code != 200:
            err = 'HTTP %s: %s' % (response.status_code, (response.text or '')[:400])
            _logger.error('Hub OAuth: install falló en %s — %s', url, err)
            tenant.sudo().write({'last_error': err[:255]})
            return False, err
        tenant.sudo().write({
            'last_authorized_at': fields.Datetime.now(),
            'last_error': False,
        })
        return True, None

    @http.route('/tiendanube/oauth/authorize', type='http', auth='user', website=True)
    def authorize(self, **kwargs):
        """Inicia el flujo OAuth de TiendaNube (directo o redirige al hub)."""
        try:
            config = request.env['tn.config'].get_config()
            if not config:
                return self._page_error(
                    'No hay configuración de TiendaNube para esta compañía.',
                    title='Error de Configuración',
                )
            sec = config.sudo()

            # Si hay hub configurado y esta base no es el hub → siempre vía hub.
            # (Ignora Client ID/Secret locales residuales; esos solo sirven en modo directo.)
            use_hub = sec._oauth_via_hub_enabled() and not sec._oauth_this_is_hub()
            if use_hub and sec._oauth_same_host(sec._oauth_hub_url(), request.httprequest.host_url):
                # Misma base mal detectada: no rebotar a /hub/start; autorizar local.
                use_hub = False
            if use_hub:
                return self._redirect_tenant_to_hub(sec)

            has_local_app = bool((sec.client_id or '').strip() and (sec.client_secret or '').strip())
            if not has_local_app:
                return self._page_error(
                    'Debe configurar Client ID y Client Secret (modo directo), '
                    'o los parámetros oauth_hub_url / oauth_shared_secret (modo hub).',
                    title='Error de Configuración',
                )

            code_verifier = secrets.token_urlsafe(32)
            oauth_state = secrets.token_urlsafe(32)
            request.session['tn_code_verifier'] = code_verifier
            request.session['tn_config_id'] = config.id
            request.session['tn_oauth_state'] = oauth_state

            # Directo: misma redirect URI que siempre (host actual).
            redirect_uri = request.httprequest.host_url.rstrip('/') + '/tiendanube/oauth/callback'
            return self._redirect_to_tiendanube(sec.client_id, oauth_state, redirect_uri)

        except Exception as e:
            _logger.exception('Error al iniciar autorización OAuth: %s', e)
            return self._page_error('Error al iniciar autorización: %s' % e)

    @http.route('/tiendanube/oauth/hub/start', type='http', auth='public', csrf=False, website=True)
    def hub_start(self, **kwargs):
        """El cliente llega acá firmado; el hub redirige a TiendaNube con su callback fijo."""
        tenant_code = (kwargs.get('tenant') or '').strip()
        return_url = (kwargs.get('return_url') or '').rstrip('/')
        ts = kwargs.get('ts')
        sig = kwargs.get('sig')

        Tenant = request.env['tn.oauth.tenant'].sudo()
        if not Tenant._verify_start_signature(tenant_code, return_url, ts, sig):
            _logger.warning('Hub OAuth start: firma inválida tenant=%s', tenant_code)
            return self._page_error('Firma inválida o solicitud vencida. Volvé a autorizar desde Odoo.')
        if not tenant_code or not return_url:
            return self._page_error('Faltan datos para iniciar la autorización.')

        tenant = Tenant._get_or_create_for_start(tenant_code, return_url)
        if not tenant.active:
            return self._page_error('Esta base no está habilitada para conectar TiendaNube.')
        if not tenant._return_url_allowed(return_url):
            _logger.warning(
                'Hub OAuth start: return_url no permitido %s (base=%s)',
                return_url, tenant.base_url,
            )
            return self._page_error('La URL de esta base no coincide con la registrada.')

        hub_config = request.env['tn.config'].sudo().get_config()
        if not hub_config or not hub_config.client_id or not hub_config.client_secret:
            return self._page_error(
                'El hub no tiene Client ID / Client Secret de la app TiendaNube. '
                'Cargalos en TiendaNube → Configuración de ESTA base (el hub), '
                'o si el cliente debería autorizar directo, vaciá oauth_hub_url y '
                'oauth_shared_secret en los parámetros del sistema del cliente.'
            )
        if not hub_config._oauth_this_is_hub():
            _logger.warning('Hub OAuth start: esta base no es el hub (web.base.url ≠ oauth_hub_url)')

        Pending = request.env['tn.oauth.pending'].sudo()
        Pending._purge_expired()
        oauth_state = secrets.token_urlsafe(32)
        Pending.create({
            'state_token': oauth_state,
            'tenant_id': tenant.id,
            'return_url': return_url,
        })
        redirect_uri = self._callback_uri()
        return self._redirect_to_tiendanube(hub_config.client_id, oauth_state, redirect_uri)

    @http.route('/tiendanube/oauth/callback', type='http', auth='public', csrf=False, website=True)
    def callback(self, **kwargs):
        """Callback único de TiendaNube: flujo hub (pending) o directo (sesión)."""
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

        if error:
            _logger.error('Error en callback OAuth: %s', error)
            _clear_oauth_session()
            return self._page_error('Error de autorización: %s' % error)

        if not code:
            _clear_oauth_session()
            return self._page_error("Falta el parametro 'code' en la respuesta de TiendaNube.")

        pending = request.env['tn.oauth.pending'].sudo()._get_valid(state_param)
        if pending:
            return self._callback_hub(pending, code, _clear_oauth_session)

        if expected_state and state_param:
            if not _tn_oauth_state_matches(state_param, expected_state):
                _logger.warning('Callback OAuth: state invalido (posible CSRF)')
                _clear_oauth_session()
                return self._page_error(
                    'Validacion de seguridad fallida (parametro state). '
                    'Inicie la autorizacion de nuevo desde Odoo.'
                )

        if config_id:
            config = request.env['tn.config'].sudo().browse(int(config_id))
        else:
            config = request.env['tn.config'].sudo().get_config()

        if not config or not config.exists():
            _clear_oauth_session()
            return self._page_error('No se encontro ninguna configuracion de TiendaNube en Odoo.')

        # Instalación desde el admin de TN sin state: no en el hub (evitar tokens en la base central).
        if not expected_state and config._oauth_this_is_hub():
            _clear_oauth_session()
            return self._page_error(
                'Esta base es el hub OAuth. Conectá TiendaNube desde el Odoo de la tienda.'
            )

        if not config.client_id or not config.client_secret:
            _clear_oauth_session()
            return self._page_error('La configuracion no tiene Client ID / Client Secret cargados.')

        try:
            response = self._exchange_code(
                config.client_id,
                config.client_secret,
                code,
                code_verifier=code_verifier,
            )
            if response.status_code == 200:
                self._apply_tokens(config, response.json())
                _clear_oauth_session()
                _logger.info('Tokens obtenidos correctamente (store_id=%s)', config.store_id)
                return _oauth_html(
                    'Autorizacion exitosa',
                    'Autorizacion exitosa',
                    'La tienda quedo conectada con Odoo. Podes cerrar esta ventana.',
                )
            _logger.error('Error al obtener tokens: %s - %s', response.status_code, response.text)
            _clear_oauth_session()
            return self._page_error('Error %s: %s' % (response.status_code, response.text))
        except Exception as e:
            _logger.exception('Error en callback OAuth: %s', e)
            _clear_oauth_session()
            return self._page_error('Error al procesar la autorizacion: %s' % e)

    def _callback_hub(self, pending, code, clear_session):
        tenant = pending.tenant_id
        hub_config = request.env['tn.config'].sudo().get_config()
        if not hub_config or not hub_config.client_id or not hub_config.client_secret:
            pending.write({'consumed': True})
            clear_session()
            return self._page_error('El hub no tiene Client ID / Client Secret.')

        try:
            response = self._exchange_code(
                hub_config.client_id,
                hub_config.client_secret,
                code,
                redirect_uri=self._callback_uri(),
            )
            pending.write({'consumed': True})
            clear_session()
            if response.status_code != 200:
                tenant.write({'last_error': ('Token %s: %s' % (response.status_code, response.text))[:255]})
                return self._page_error('Error %s: %s' % (response.status_code, response.text))

            token_data = response.json()
            ok, err = self._push_tokens_to_tenant(tenant, token_data)
            if not ok:
                return self._page_error(
                    'Se obtuvieron los tokens pero no se pudieron enviar a %s. %s'
                    % (tenant.name, err or '')
                )
            done_url = '%s/tiendanube/oauth/done' % (pending.return_url or tenant._normalized_base_url())
            return request.redirect(done_url, local=False)
        except Exception as e:
            _logger.exception('Hub OAuth callback: %s', e)
            pending.write({'consumed': True})
            tenant.write({'last_error': str(e)[:255]})
            clear_session()
            return self._page_error('Error al procesar la autorizacion: %s' % e)

    @http.route('/tiendanube/oauth/install', type='http', auth='public', csrf=False, methods=['POST'])
    def install_from_hub(self, **kwargs):
        """El hub POSTea los tokens acá. No van por querystring ni por el browser."""
        raw = request.httprequest.data or b''
        signature = request.httprequest.headers.get('X-TN-OAuth-Signature') or ''
        config = request.env['tn.config'].sudo().get_config()

        def _json(payload, status=200):
            return request.make_response(
                json.dumps(payload),
                headers={'Content-Type': 'application/json'},
                status=status,
            )

        if not config:
            return _json({'ok': False, 'error': 'no_config'}, status=400)
        if not config._oauth_via_hub_enabled() or config._oauth_this_is_hub():
            return _json({'ok': False, 'error': 'not_store'}, status=400)
        if not config._verify_oauth_install_signature(raw, signature):
            _logger.warning('OAuth install: firma HMAC inválida')
            return _json({'ok': False, 'error': 'bad_signature'}, status=403)
        try:
            data = json.loads(raw.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            return _json({'ok': False, 'error': 'bad_json'}, status=400)
        try:
            ts = int(data.get('ts') or 0)
        except (TypeError, ValueError):
            ts = 0
        if abs(time.time() - ts) > _INSTALL_TS_TTL:
            return _json({'ok': False, 'error': 'expired'}, status=403)
        if not data.get('access_token'):
            return _json({'ok': False, 'error': 'no_token'}, status=400)
        self._apply_tokens(config, data)
        _logger.info('OAuth install: tokens recibidos del hub (store_id=%s)', config.store_id)
        return _json({'ok': True, 'store_id': config.store_id})

    @http.route('/tiendanube/oauth/done', type='http', auth='public', csrf=False, website=True)
    def oauth_done(self, **kwargs):
        return _oauth_html(
            'Autorizacion exitosa',
            'Autorizacion exitosa',
            'La tienda quedo conectada con Odoo. Podes cerrar esta ventana.',
        )
