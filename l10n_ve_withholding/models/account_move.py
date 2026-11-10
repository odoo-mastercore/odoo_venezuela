# -*- coding: utf-8 -*-
##############################################################################
# Author: SINAPSYS GLOBAL SA || MASTERCORE SAS
# Copyleft: 2020-Present.
# License LGPL-3.0 or later (http: //www.gnu.org/licenses/lgpl.html).
#
#
###############################################################################
from odoo import models, fields, api, _, Command
from odoo.exceptions import UserError, ValidationError


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_ve_control_number = fields.Char(
        'Control Number',
        size=80,
        store=True,
        help="Number used to manage pre-printed invoices, by law you will"
             " need to put here this number to be able to declarate on"
             " Fiscal reports correctly.",
        copy=False
    )
    l10n_ve_withholding_ids = fields.Many2many(
        string='Withholdings',
        comodel_name='l10n_ve.payment.withholding',
        relation='payment_withholding_move_rel',
        column1='withholding_id',
        column2='move_id',
        copy=False,
    )
    l10n_ve_customer_vat_withholding = fields.Boolean(
        related='company_id.l10n_ve_customer_vat_withholding',
    )

    def _get_l10n_ve_customer_vat_lines(self):
        self.ensure_one()
        groups = self.env['account.tax.group']
        for company in self.company_id.parent_ids:
            for rate in ('0', '8', '16', '31'):
                groups |= self.env.ref(
                    f'account.{company.id}_tax_group_iva_{rate}',
                    raise_if_not_found=False,
                ) or self.env['account.tax.group']
        return self.line_ids.filtered(
            lambda line: line.tax_line_id and line.tax_group_id in groups
        )

    def _check_l10n_ve_customer_vat_withholding(self):
        self.ensure_one()
        if (
            self.move_type != 'out_invoice'
            or self.state != 'posted'
            or self.country_code != 'VE'
            or not self.company_id.l10n_ve_customer_vat_withholding
            or not self.company_id.use_payment_pro
        ):
            raise UserError(_(
                'La retención de IVA requiere una factura de cliente publicada '
                'y la configuración de retenciones y pagos avanzados activa en la compañía.'
            ))

    def action_apply_l10n_ve_customer_vat_withholding(self):
        self._check_l10n_ve_customer_vat_withholding()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Aplicar Retención IVA'),
            'res_model': 'l10n_ve.customer.vat.withholding.wizard',
            'view_mode': 'form',
            'views': [(self.env.ref('l10n_ve_withholding.view_customer_vat_withholding_wizard').id, 'form')],
            'target': 'new',
            'context': dict(self.env.context, default_move_id=self.id),
        }

    def _create_l10n_ve_customer_vat_withholding_payment(self, number):
        self._check_l10n_ve_customer_vat_withholding()
        number = (number or '').strip()
        if not number or number == '/':
            raise UserError(_('Indique el número de comprobante de retención.'))
        # Serialize confirmations on this invoice before creating its retention payment.
        self.lock_for_update()
        payments = self.env['account.payment'].search([
            ('company_id', '=', self.company_id.id),
            ('state', '!=', 'canceled'),
            '|',
            ('l10n_ve_customer_vat_invoice_id', '=', self.id),
            ('to_pay_move_line_ids.move_id', '=', self.id),
        ])
        payments |= self.l10n_ve_withholding_ids.payment_id.filtered(
            lambda payment: payment.state != 'canceled'
        )
        vat_payments = payments.filtered(lambda payment: any(
            line.tax_id.l10n_ve_withholding_payment_type == 'customer'
            and (
                line.tax_id.l10n_ve_withholding_ingoing_type == 'iva'
                or line.tax_id.l10n_ve_tax_type == 'partner_tax'
            )
            for line in payment._get_l10n_ve_active_withholding_lines()
        ))
        if vat_payments:
            raise UserError(_('Esta factura ya tiene una retención de IVA registrada.'))
        debt = self.line_ids.filtered(
            lambda line: line.account_id.account_type == 'asset_receivable' and not line.reconciled
        )
        if not debt or self.currency_id.is_zero(self.amount_residual):
            raise UserError(_('La factura no tiene deuda pendiente para aplicar una retención.'))
        company_currency = self.company_id.currency_id
        base_amount = company_currency.round(-sum(self._get_l10n_ve_customer_vat_lines().mapped('balance')))
        amount = company_currency.round(
            base_amount * int(self.company_id.l10n_ve_customer_vat_withholding_rate) / 100.0
        )
        if amount <= 0.0:
            raise UserError(_('La factura no tiene IVA contable sujeto a retención.'))
        invoice_rate = self.invoice_currency_rate if self.currency_id != company_currency else 1.0
        if not invoice_rate:
            raise UserError(_('La factura no tiene una tasa de cambio válida.'))
        if self.currency_id.compare_amounts(amount * invoice_rate, self.amount_residual) > 0:
            raise UserError(_('La retención calculada supera el saldo pendiente de la factura.'))
        taxes = self.env['account.tax'].search([
            ('company_id', 'parent_of', self.company_id.id),
            ('l10n_ve_withholding_payment_type', '=', 'customer'),
            '|',
            ('l10n_ve_withholding_ingoing_type', '=', 'iva'),
            ('l10n_ve_tax_type', '=', 'partner_tax'),
        ])
        taxes = taxes.filtered(lambda tax: tax.company_id == self.company_id) or taxes
        if len(taxes) != 1:
            raise UserError(_(
                'Configure un único impuesto de retención de IVA en ventas para esta compañía.'
            ))
        method = self.env['account.payment.method.line'].search([
            ('journal_id.company_id', '=', self.company_id.id),
            ('journal_id.type', 'in', ('bank', 'cash')),
            ('journal_id.active', '=', True),
            ('journal_id.currency_id', 'in', [False, company_currency.id]),
            ('payment_method_id.code', '=', 'manual'),
            ('payment_method_id.payment_type', '=', 'inbound'),
        ], limit=1)
        if not method:
            raise UserError(_(
                'Configure un diario de banco o efectivo en moneda de la compañía con un método de cobro manual.'
            ))
        date = fields.Date.context_today(self)
        # The wizard's default_move_id must not become the payment's journal entry.
        context = {key: value for key, value in self.env.context.items() if not key.startswith('default_')}
        payment = self.env['account.payment'].with_context(context).with_company(self.company_id).create({
            'l10n_ve_customer_vat_invoice_id': self.id,
            'company_id': self.company_id.id,
            'partner_id': self.commercial_partner_id.id,
            'partner_type': 'customer',
            'payment_type': 'inbound',
            'journal_id': method.journal_id.id,
            'payment_method_line_id': method.id,
            'currency_id': company_currency.id,
            'date': date,
            'amount': 0.0,
            'amount_exact': 0.0,
            'counterpart_currency_id': self.currency_id.id,
            'to_pay_move_line_ids': [Command.set(debt.ids)],
            'l10n_ve_withholding_line_ids': [Command.create({
                'tax_id': taxes.id,
                'name': number,
                'date': date,
                'base_amount': base_amount,
                'amount': amount,
            })],
        })
        if not payment.use_payment_pro:
            raise UserError(_(
                'Configure la cuenta de cobros pendientes del diario para registrar la retención.'
            ))
        payment.action_post()
        counterparts = payment.move_id.line_ids.filtered(
            lambda line: line.account_id == payment.destination_account_id
        )
        partials = (counterparts.matched_debit_ids | counterparts.matched_credit_ids).filtered(
            lambda partial: self in (partial.debit_move_id.move_id | partial.credit_move_id.move_id)
        )
        if payment.move_id.state != 'posted' or company_currency.compare_amounts(
            sum(partials.mapped('amount')), amount,
        ):
            raise UserError(_(
                'No fue posible publicar y aplicar íntegramente la retención a la factura. '
                'No se guardó el pago; revise la configuración contable.'
            ))
        return payment

    def action_register_payment(self):
        action = super().action_register_payment()
        supplier_moves = self.filtered(
            lambda move: move.move_type in ('in_invoice', 'in_refund')
        )
        if supplier_moves and isinstance(action, dict):
            context = dict(action.get('context') or {})
            context['l10n_ve_auto_withhold_move_ids'] = supplier_moves.ids
            action['context'] = context
        return action

    def _post(self, soft=True):
        moves_posted = super(AccountMove, self)._post(soft)
        for move in moves_posted:
            if (move.state == 'posted' and move.l10n_ve_control_number == False) or \
                (move.move_type == 'out_refund' and move.l10n_ve_control_number == ''):
                if move.move_type in ['out_invoice', 'out_refund']:
                    if move.journal_id.l10n_ve_sequence_control_id:
                        l10n_ve_control_number = move.env['ir.sequence'].next_by_code(
                            move.journal_id.l10n_ve_sequence_control_id.code)
                        move.write({
                            'l10n_ve_control_number': l10n_ve_control_number
                        })
                    else:
                        raise ValidationError(_(
                            "El diario por el cual está emitiendo la factura no " +
                            "tiene secuencia para número de control"
                        ))
        return moves_posted

    def get_exempt_amount(self):
        self.ensure_one()
        exempt_amount = 0.0
        for line in self.invoice_line_ids:
            if line.tax_ids and line.tax_ids.filtered(lambda r: r.amount == 0):
                exempt_amount += line.price_subtotal
        if self.currency_id != self.company_id.currency_id:
            exempt_amount = exempt_amount * self.inverse_invoice_currency_rate
        return abs(exempt_amount)
