# -*- coding: utf-8 -*-
##############################################################################
# Author: SINAPSYS GLOBAL SA || MASTERCORE SAS
# Copyleft: 2020-Present.
# License LGPL-3.0 or later (http: //www.gnu.org/licenses/lgpl.html).
#
#
###############################################################################
from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import SQL


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
    l10n_ve_has_customer_vat_withholding = fields.Boolean(
        compute='_compute_l10n_ve_has_customer_vat_withholding',
    )

    @api.depends(
        'move_type', 'l10n_ve_withholding_ids.state', 'l10n_ve_withholding_ids.amount',
        'l10n_ve_withholding_ids.payment_id.state', 'l10n_ve_withholding_ids.payment_id.move_id.state',
        'l10n_ve_withholding_ids.tax_id.l10n_ve_tax_type',
        'l10n_ve_withholding_ids.tax_id.l10n_ve_withholding_payment_type',
        'l10n_ve_withholding_ids.tax_id.l10n_ve_withholding_ingoing_type',
    )
    def _compute_l10n_ve_has_customer_vat_withholding(self):
        for move in self:
            move.l10n_ve_has_customer_vat_withholding = move.move_type == 'out_invoice' and any(
                line.state == 'posted'
                and line.amount > 0.0
                and line.payment_id.state not in ('canceled', 'rejected')
                and line.payment_id.move_id.state == 'posted'
                and line.tax_id._is_l10n_ve_customer_vat_withholding()
                for line in move.l10n_ve_withholding_ids
            )

    def _get_l10n_ve_customer_vat_lines(self):
        self.ensure_one()
        groups = self.env['account.tax.group']
        for company in self.company_id.parent_ids:
            for rate in ('0', '8', '16', '31'):
                groups |= self.env.ref(
                    f'account.{company.id}_tax_group_iva_{rate}', raise_if_not_found=False,
                ) or self.env['account.tax.group']
        return self.line_ids.filtered(lambda line: line.tax_line_id and line.tax_group_id in groups)

    def _check_l10n_ve_customer_vat_withholding(self):
        self.ensure_one()
        if (
            self.move_type != 'out_invoice' or self.state != 'posted' or self.country_code != 'VE'
            or not self.l10n_ve_customer_vat_withholding or not self.company_id.use_payment_pro
        ):
            raise UserError(_(
                'La retención de IVA requiere una factura de cliente publicada y que la compañía '
                'tenga activados los pagos avanzados y la retención de IVA de clientes.'
            ))
        if self.l10n_ve_has_customer_vat_withholding:
            raise UserError(_('Esta factura ya tiene una retención de IVA aplicada.'))
        if self.currency_id.compare_amounts(self.amount_residual, 0.0) <= 0:
            raise UserError(_('La factura no tiene deuda pendiente para aplicar una retención.'))

        journal = self.company_id.l10n_ve_customer_vat_withholding_journal_id
        if (
            not journal or not journal.active or journal.company_id != self.company_id
            or journal.type not in ('bank', 'cash')
            or (journal.currency_id and journal.currency_id != self.company_currency_id)
        ):
            raise UserError(_(
                'Configure un diario activo de banco o efectivo en la moneda de esta compañía '
                'para la retención de IVA de clientes.'
            ))
        return journal

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

    def _lock_l10n_ve_customer_vat_invoice(self):
        self.ensure_one()
        self.check_access('write')
        # Serialize competing confirmations on the same invoice.
        self.env.cr.execute(SQL('SELECT id FROM account_move WHERE id = %s FOR UPDATE', self.id))

    def _check_l10n_ve_customer_vat_duplicate(self, exclude_payment=None):
        self.ensure_one()
        payments = self.env['account.payment'].search([
            ('company_id', '=', self.company_id.id),
            ('state', 'not in', ('canceled', 'rejected')),
            '|', ('l10n_ve_customer_vat_invoice_id', '=', self.id),
            ('to_pay_move_line_ids.move_id', '=', self.id),
        ])
        payments |= self.l10n_ve_withholding_ids.payment_id.filtered(
            lambda payment: payment.state not in ('canceled', 'rejected')
        )
        if exclude_payment:
            payments -= exclude_payment
        if any(
            line.tax_id._is_l10n_ve_customer_vat_withholding()
            and (line.amount > 0.0 or (line.name and line.name.strip() not in ('', '/')))
            for line in payments.l10n_ve_withholding_line_ids
            if line.state != 'cancel'
        ):
            raise UserError(_('Esta factura ya tiene una retención de IVA registrada.'))

    def _get_l10n_ve_customer_vat_allocation_rate(self):
        self.ensure_one()
        return self.invoice_currency_rate if self.currency_id != self.company_currency_id else 1.0

    def _create_l10n_ve_customer_vat_withholding_payment(self, number):
        self.ensure_one()
        number = (number or '').strip()
        if not number or number == '/':
            raise UserError(_('Indique el número de comprobante de retención.'))
        self._lock_l10n_ve_customer_vat_invoice()
        journal = self._check_l10n_ve_customer_vat_withholding()
        self._check_l10n_ve_customer_vat_duplicate()

        debt = self.line_ids.filtered(
            lambda line: line.account_id.account_type == 'asset_receivable'
            and self.currency_id.compare_amounts(line.amount_residual_currency, 0.0) > 0
        )
        if not debt:
            raise UserError(_('La factura no tiene deuda pendiente para aplicar una retención.'))
        company_currency = self.company_currency_id
        base_amount = company_currency.round(-sum(self._get_l10n_ve_customer_vat_lines().mapped('balance')))
        percentage = self.company_id.l10n_ve_customer_vat_withholding_rate
        amount = company_currency.round(base_amount * int(percentage) / 100.0)
        if amount <= 0.0:
            raise UserError(_('La factura no tiene IVA contable sujeto a retención.'))
        rate = self._get_l10n_ve_customer_vat_allocation_rate()
        if rate <= 0.0 or self.currency_id.is_zero(amount * rate):
            raise UserError(_('La retención no tiene un importe válido en la moneda de la factura.'))
        if self.currency_id.compare_amounts(amount * rate, self.amount_residual) > 0:
            raise UserError(_('La retención calculada supera el saldo pendiente de la factura.'))

        taxes = self.env['account.tax'].search([
            ('company_id', 'parent_of', self.company_id.id),
            ('l10n_ve_withholding_payment_type', '=', 'customer'),
            '|', ('l10n_ve_withholding_ingoing_type', '=', 'iva'), ('l10n_ve_tax_type', '=', 'partner_tax'),
        ])
        taxes = taxes.filtered(lambda tax: tax.company_id == self.company_id) or taxes
        if len(taxes) != 1:
            raise UserError(_('Configure un único impuesto de retención de IVA en ventas para esta compañía.'))

        method = journal.inbound_payment_method_line_ids.filtered(lambda line: line.code == 'manual')[:1]
        if not method or not method.payment_account_id:
            raise UserError(_('Configure un método de cobro manual con cuenta de cobros pendientes en el diario.'))

        date = fields.Date.context_today(self)
        context = {key: value for key, value in self.env.context.items() if not key.startswith('default_')}
        Payment = self.env['account.payment'].with_context(context).with_company(self.company_id)
        vals = {
            'l10n_ve_customer_vat_invoice_id': self.id,
            'company_id': self.company_id.id,
            'partner_id': self.commercial_partner_id.id,
            'partner_type': 'customer',
            'payment_type': 'inbound',
            'journal_id': journal.id,
            'payment_method_line_id': method.id,
            'currency_id': company_currency.id,
            'date': date,
            'amount': 0.0,
            'to_pay_move_line_ids': [Command.set(debt.ids)],
            'l10n_ve_withholding_line_ids': [Command.create({
                'tax_id': taxes.id,
                'name': number,
                'date': date,
                'base_amount': base_amount,
                'amount': amount,
                'ref': f'({base_amount} * {percentage}%)',
            })],
        }
        if 'amount_exact' in Payment._fields:
            vals['amount_exact'] = 0.0
        if self.currency_id != company_currency:
            vals.update(counterpart_currency_id=self.currency_id.id, counterpart_exchange_rate=1.0 / rate)
        payment = Payment.create(vals)
        if not payment.use_payment_pro:
            raise UserError(_('El diario no permite publicar el pago mediante Pagos Avanzados.'))
        payment.action_post()

        counterpart = payment.move_id.line_ids.filtered(lambda line: line.account_id == payment.destination_account_id)
        partials = counterpart.matched_debit_ids.filtered(lambda part: part.debit_move_id in debt)
        if (
            payment.move_id.state != 'posted'
            or company_currency.compare_amounts(sum(partials.mapped('amount')), amount)
            or self.currency_id.compare_amounts(sum(partials.mapped('debit_amount_currency')), amount * rate)
        ):
            raise UserError(_('No se pudo publicar y aplicar íntegramente la retención. No se guardó el pago.'))
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
