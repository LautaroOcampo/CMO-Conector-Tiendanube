# -*- coding: utf-8 -*-
import hashlib
import hmac
import time
from datetime import timedelta
from urllib.parse import urlparse

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_PENDING_TTL_SECONDS = 15 * 60
_START_TTL_SECONDS = 5 * 60


class TnOauthTenant(models.Model):
    _name = 'tn.oauth.tenant'
    _description = 'Cliente OAuth TiendaNube (hub)'
    _order = 'name'

    name = fields.Char(required=True)
    code = fields.Char(
        string='Código',
        required=True,
        copy=False,
        help='Identificador que el Odoo cliente manda al iniciar OAuth (dbname o slug).',
    )
    base_url = fields.Char(
        string='URL del Odoo cliente',
        required=True,
        help='Ej: https://cliente.odoo.com (sin barra final). Allowlist del return_url.',
    )
    shared_secret = fields.Char(
        string='Secreto compartido',
        copy=False,
        groups='tiendanube_connector.group_tn_manager',
    )
    active = fields.Boolean(default=True)
    last_authorized_at = fields.Datetime(readonly=True, copy=False)
    last_error = fields.Char(readonly=True, copy=False)
    config_id = fields.Many2one(
        'tn.config',
        string='Configuración',
        ondelete='cascade',
        index=True,
        default=lambda self: self.env['tn.config'].get_config(),
    )

    _code_uniq = models.Constraint(
        'UNIQUE(code)',
        'El código de cliente OAuth debe ser único.',
    )

    @api.constrains('code')
    def _check_code(self):
        for rec in self:
            code = (rec.code or '').strip()
            if not code or ' ' in code:
                raise ValidationError(_('El código no puede tener espacios.'))

    def write(self, vals):
        if vals.get('code'):
            vals['code'] = vals['code'].strip()
        if vals.get('base_url'):
            vals['base_url'] = vals['base_url'].rstrip('/')
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('code'):
                vals['code'] = vals['code'].strip()
            if vals.get('base_url'):
                vals['base_url'] = vals['base_url'].rstrip('/')
        return super().create(vals_list)

    def _normalized_base_url(self):
        self.ensure_one()
        return (self.base_url or '').rstrip('/')

    def _return_url_allowed(self, return_url):
        """El return_url debe ser del mismo host que base_url (https)."""
        self.ensure_one()
        if not return_url:
            return False
        allowed = urlparse(self._normalized_base_url())
        got = urlparse(return_url)
        if got.scheme not in ('https', 'http'):
            return False
        return (got.netloc or '').lower() == (allowed.netloc or '').lower()

    def _hub_secret(self):
        return self.env['tn.config']._oauth_hub_secret()

    def _sign(self, message):
        secret = (self._hub_secret() or '').encode('utf-8')
        return hmac.new(secret, message.encode('utf-8'), hashlib.sha256).hexdigest()

    @api.model
    def _verify_start_signature(self, tenant_code, return_url, ts, sig):
        try:
            ts_int = int(ts)
        except (TypeError, ValueError):
            return False
        if abs(time.time() - ts_int) > _START_TTL_SECONDS:
            return False
        secret = (self.env['tn.config']._oauth_hub_secret() or '').encode('utf-8')
        if not secret:
            return False
        expected = hmac.new(
            secret,
            ('%s|%s|%s' % (tenant_code, return_url, ts)).encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()
        try:
            return hmac.compare_digest(expected, str(sig or ''))
        except (TypeError, ValueError):
            return False

    def _sign_body(self, raw_body):
        secret = (self._hub_secret() or '').encode('utf-8')
        if isinstance(raw_body, str):
            raw_body = raw_body.encode('utf-8')
        return hmac.new(secret, raw_body, hashlib.sha256).hexdigest()

    @api.model
    def _get_or_create_for_start(self, tenant_code, return_url):
        tenant = self.search([('code', '=', tenant_code)], limit=1)
        if tenant:
            return tenant
        config = self.env['tn.config'].get_config()
        return self.create({
            'name': tenant_code,
            'code': tenant_code,
            'base_url': return_url,
            'config_id': config.id if config else False,
        })


class TnOauthPending(models.Model):
    _name = 'tn.oauth.pending'
    _description = 'Sesión OAuth TiendaNube pendiente (hub)'
    _order = 'create_date desc'

    state_token = fields.Char(required=True, index=True, copy=False)
    tenant_id = fields.Many2one('tn.oauth.tenant', required=True, ondelete='cascade')
    return_url = fields.Char(required=True)
    consumed = fields.Boolean(default=False, copy=False)

    _state_uniq = models.Constraint(
        'UNIQUE(state_token)',
        'El state OAuth debe ser único.',
    )

    @api.model
    def _purge_expired(self):
        cutoff = fields.Datetime.now() - timedelta(seconds=_PENDING_TTL_SECONDS)
        expired = self.sudo().search([
            '|',
            ('consumed', '=', True),
            ('create_date', '<', cutoff),
        ])
        if expired:
            expired.unlink()

    @api.model
    def _get_valid(self, state_token):
        if not state_token:
            return self.browse()
        cutoff = fields.Datetime.now() - timedelta(seconds=_PENDING_TTL_SECONDS)
        return self.sudo().search([
            ('state_token', '=', state_token),
            ('consumed', '=', False),
            ('create_date', '>=', cutoff),
        ], limit=1)
