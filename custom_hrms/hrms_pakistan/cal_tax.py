import unicodedata
from datetime import date

import frappe
from frappe import _, msgprint
from frappe.model.naming import make_autoname
from frappe.query_builder import Order
from frappe.query_builder.functions import Count, Sum
from frappe.utils import (
	add_days,
	ceil,
	cint,
	cstr,
	date_diff,
	floor,
	flt,
	formatdate,
	get_first_day,
	get_last_day,
	get_link_to_form,
	getdate,
	money_in_words,
	rounded,
)
from frappe.utils.background_jobs import enqueue

import erpnext
from erpnext.accounts.utils import get_fiscal_year
from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee
from erpnext.utilities.transaction_base import TransactionBase

from hrms.hr.utils import validate_active_employee
from hrms.payroll.doctype.additional_salary.additional_salary import get_additional_salaries
from hrms.payroll.doctype.employee_benefit_application.employee_benefit_application import (
	get_benefit_component_amount,
)
from hrms.payroll.doctype.employee_benefit_claim.employee_benefit_claim import (
	get_benefit_claim_amount,
	get_last_payroll_period_benefits,
)
from hrms.payroll.doctype.payroll_entry.payroll_entry import get_salary_withholdings, get_start_end_dates
from hrms.payroll.doctype.payroll_period.payroll_period import (
	get_payroll_period,
	get_period_factor,
)
from hrms.payroll.doctype.salary_slip.salary_slip_loan_utils import (
	cancel_loan_repayment_entry,
	make_loan_repayment_entry,
	process_loan_interest_accrual_and_demand,
	set_loan_repayment,
)
from hrms.payroll.utils import sanitize_expression
from hrms.utils.holiday_list import get_holiday_dates_between

# TODO: Ensure this import path is correct and the module exists
# from hrms.hr.utils import calculate_tax_with_marginal_relief
# Import or define eval_tax_slab_condition and calculate_tax_with_marginal_relief
try:
	from hrms.hr.utils import eval_tax_slab_condition, calculate_tax_with_marginal_relief
except ImportError:
	def eval_tax_slab_condition(*args, **kwargs):
		raise NotImplementedError("eval_tax_slab_condition is not implemented or imported.")
	def calculate_tax_with_marginal_relief(*args, **kwargs):
		raise NotImplementedError("calculate_tax_with_marginal_relief is not implemented or imported.")

# cache keys
HOLIDAYS_BETWEEN_DATES = "holidays_between_dates"
LEAVE_TYPE_MAP = "leave_type_map"
SALARY_COMPONENT_VALUES = "salary_component_values"
TAX_COMPONENTS_BY_COMPANY = "tax_components_by_company"


class SalarySlip(TransactionBase):
	def calculate_variable_tax(self, tax_component):
		frappe.log_error("calculate_variable_tax: custom")
		frappe.msgprint("calculate_variable_tax: custom")
		self.previous_total_paid_taxes = self.get_tax_paid_in_period(
			self.payroll_period.start_date, self.start_date, tax_component
		)

		# Structured tax amount
		eval_locals, default_data = self.get_data_for_eval()
		self.total_structured_tax_amount, __ = self.calculate_tax_by_tax_slab(
			self.total_taxable_earnings_without_full_tax_addl_components,
			self.tax_slab,
			self.whitelisted_globals,
			eval_locals,
		)

		self.current_structured_tax_amount = (
			self.total_structured_tax_amount - self.previous_total_paid_taxes
		) / self.remaining_sub_periods

		# Total taxable earnings with additional earnings with full tax
		self.full_tax_on_additional_earnings = 0.0
		if self.current_additional_earnings_with_full_tax:
			self.total_tax_amount, __ = self.calculate_tax_by_tax_slab(
				self.total_taxable_earnings, self.tax_slab, self.whitelisted_globals, eval_locals
			)
			self.full_tax_on_additional_earnings = self.total_tax_amount - self.total_structured_tax_amount

		current_tax_amount = self.current_structured_tax_amount + self.full_tax_on_additional_earnings
		if flt(current_tax_amount) < 0:
			current_tax_amount = 0

		# Ensure the dict exists before updating
		if not hasattr(self, '_component_based_variable_tax'):
			self._component_based_variable_tax = {}
		if tax_component not in self._component_based_variable_tax:
			self._component_based_variable_tax[tax_component] = {}

		self._component_based_variable_tax[tax_component].update(
			{
				"previous_total_paid_taxes": self.previous_total_paid_taxes,
				"total_structured_tax_amount": self.total_structured_tax_amount,
				"current_structured_tax_amount": self.current_structured_tax_amount,
				"full_tax_on_additional_earnings": self.full_tax_on_additional_earnings,
				"current_tax_amount": current_tax_amount,
			}
		)

		return current_tax_amount

	def calculate_tax_by_tax_slab(self, annual_taxable_earning, tax_slab, eval_globals=None, eval_locals=None):
		tax_amount = 0
		total_other_taxes_and_charges = 0

		frappe.log_error("calculate_tax_by_tax_slab: custom")
		frappe.msgprint("calculate_tax_by_tax_slab: custom")

		if annual_taxable_earning > tax_slab.tax_relief_limit:
			eval_locals.update({"annual_taxable_earning": annual_taxable_earning})

			for slab in tax_slab.slabs:
				cond = cstr(slab.condition).strip()
				if cond and not eval_tax_slab_condition(cond, eval_globals, eval_locals):
					continue
				if not slab.to_amount and annual_taxable_earning >= slab.from_amount:
					tax_amount += (annual_taxable_earning - slab.from_amount + 1) * slab.percent_deduction * 0.01
					continue

				if annual_taxable_earning >= slab.from_amount and annual_taxable_earning < slab.to_amount:
					tax_amount += (annual_taxable_earning - slab.from_amount + 1) * slab.percent_deduction * 0.01
				elif annual_taxable_earning >= slab.from_amount and annual_taxable_earning >= slab.to_amount:
					tax_amount += (slab.to_amount - slab.from_amount + 1) * slab.percent_deduction * 0.01

			tax_with_marginal_relief = calculate_tax_with_marginal_relief(
				tax_slab, tax_amount, annual_taxable_earning
			)
			if tax_with_marginal_relief is not None:
				tax_amount = tax_with_marginal_relief

			for d in tax_slab.other_taxes_and_charges:
				if flt(d.min_taxable_income) and flt(d.min_taxable_income) > annual_taxable_earning:
					continue

				if flt(d.max_taxable_income) and flt(d.max_taxable_income) < annual_taxable_earning:
					continue
				other_taxes_and_charges = tax_amount * flt(d.percent) / 100
				tax_amount = 99
				total_other_taxes_and_charges = 99

		return tax_amount, total_other_taxes_and_charges
