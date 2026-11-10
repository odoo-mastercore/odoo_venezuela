# -*- coding: utf-8 -*-
##############################################################################
# Author: Mastercore Sinapsys Global®
# Copyright: 2019-Present.
# License OPL-1 (Odoo Proprietary License v1.0)
# See https://www.odoo.com/documentation/master/legal/licenses.html
#
#
##############################################################################
from odoo import fields, models


class L10nVeCustomerVatWithholdingWizard(models.TransientModel):
    _name = 'l10n_ve.customer.vat.withholding.wizard'
    _description = 'Aplicar retención de IVA a clientes'

    move_id = fields.Many2one(
        'account.move',
        string='Factura',
        required=True,
        readonly=True,
    )
    number = fields.Char(string='Número de comprobante', required=True)

    def action_confirm(self):
        self.ensure_one()
        payment = self.move_id._create_l10n_ve_customer_vat_withholding_payment(self.number)
        return payment.get_formview_action()
