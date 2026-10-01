# -*- coding: utf-8 -*-

from . import controllers
from . import models
from . import services
from . import wizards

# Claves compartidas con tiendanube_oauth_hub (no son XML ID).
_OAUTH_ICP_KEYS = (
    'tiendanube_connector.oauth_hub_url',
    'tiendanube_connector.oauth_shared_secret',
)


def _ensure_oauth_icp_keys(env):
    """Crea las claves OAuth solo si no existen.

    No usar XML de ir.config_parameter: el XML ID cambia con el nombre
    técnico del módulo y choca con el unique de ``key`` si el hub u otra
    instalación ya creó el mismo parámetro.
    """
    icp = env['ir.config_parameter'].sudo()
    for key in _OAUTH_ICP_KEYS:
        if not icp.search([('key', '=', key)], limit=1):
            icp.set_param(key, '')


def post_init_hook(env_or_cr, registry=None):
    """Odoo 19+: ``post_init_hook(env)``. Versiones anteriores: ``(cr, registry)``."""
    if registry is not None:
        from odoo import api, SUPERUSER_ID

        env = api.Environment(env_or_cr, SUPERUSER_ID, {})
    else:
        env = env_or_cr
    _ensure_oauth_icp_keys(env)
