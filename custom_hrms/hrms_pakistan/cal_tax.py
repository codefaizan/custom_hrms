# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt


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

# cache keys
HOLIDAYS_BETWEEN_DATES = "holidays_between_dates"
LEAVE_TYPE_MAP = "leave_type_map"
SALARY_COMPONENT_VALUES = "salary_component_values"
TAX_COMPONENTS_BY_COMPANY = "tax_components_by_company"


class SalarySlip(TransactionBase):
	# def setup_init(self, *args, **kwargs):
	# 	super().setup_init(*args, **kwargs)
	# 	self.series = f"Sal Slip/{self.employee}/.#####"
	# 	self.whitelisted_globals = {
	# 		"int": int,
	# 		"float": float,
	# 		"long": int,
	# 		"round": round,
	# 		"rounded": rounded,
	# 		"date": date,
	# 		"getdate": getdate,
	# 		"get_first_day": get_first_day,
	# 		"get_last_day": get_last_day,
	# 		"ceil": ceil,
	# 		"floor": floor,
	# 	}

	# def autoname(self):
	# 	self.name = make_autoname(self.series)

	@property
	def joining_date(self):
		if not hasattr(self, "__joining_date"):
			self.__joining_date = frappe.get_cached_value(
				"Employee",
				self.employee,
				"date_of_joining",
			)

		return self.__joining_date
	
	@property
	def relieving_date(self):
		if not hasattr(self, "__relieving_date"):
			self.__relieving_date = frappe.get_cached_value(
				"Employee",
				self.employee,
				"relieving_date",
			)

		return self.__relieving_date

	def validate_dates(self):
		self.validate_from_to_dates("start_date", "end_date")

		if not self.joining_date:
			frappe.throw(
				_("Please set the Date Of Joining for employee {0}").format(frappe.bold(self.employee_name))
			)

		if date_diff(self.end_date, self.joining_date) < 0:
			frappe.throw(_("Cannot create Salary Slip for Employee joining after Payroll Period"))

		if self.relieving_date and date_diff(self.relieving_date, self.start_date) < 0:
			frappe.throw(_("Cannot create Salary Slip for Employee who has left before Payroll Period"))

	@frappe.whitelist()
	def get_emp_and_working_day_details(self):
		"""First time, load all the components from salary structure"""
		if self.employee:
			self.set("earnings", [])
			self.set("deductions", [])
			if hasattr(self, "loans"):
				self.set("loans", [])

			if self.payroll_frequency:
				self.get_date_details()

			self.validate_dates()

			# getin leave details
			self.get_working_days_details()
			struct = self.check_sal_struct()

			if struct:
				self.set_salary_structure_doc()
				self.salary_slip_based_on_timesheet = (
					self._salary_structure_doc.salary_slip_based_on_timesheet or 0
				)
				self.set_time_sheet()
				self.pull_sal_struct()

			process_loan_interest_accrual_and_demand(self)

	def calculate_variable_tax(self, tax_component):
		frappe.msgprint("This is a placeholder message for debugging purposes. calculate_variable_tax function called.")
		self.previous_total_paid_taxes = self.get_tax_paid_in_period(
			self.payroll_period.start_date, self.start_date, tax_component
		)

		# Structured tax amount
		eval_locals, default_data = self.get_data_for_eval()
		self.total_structured_tax_amount, tax_details = calculate_tax_by_tax_slab(
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
			self.total_tax_amount, tax_details2 = calculate_tax_by_tax_slab(
				self.total_taxable_earnings, self.tax_slab, self.whitelisted_globals, eval_locals
			)
			self.full_tax_on_additional_earnings = self.total_tax_amount - self.total_structured_tax_amount

		current_tax_amount = self.current_structured_tax_amount + self.full_tax_on_additional_earnings
		if flt(current_tax_amount) < 0:
			current_tax_amount = 0

		self.component_based_variable_tax[tax_component].update(
			{
				"previous_total_paid_taxes": self.previous_total_paid_taxes,
				"total_structured_tax_amount": self.total_structured_tax_amount,
				"current_structured_tax_amount": self.current_structured_tax_amount,
				"full_tax_on_additional_earnings": self.full_tax_on_additional_earnings,
				"current_tax_amount": current_tax_amount,
			}
		)

		return current_tax_amount
	
	def get_working_days_details(self, lwp=None, for_preview=0):
		payroll_settings = frappe.get_cached_value(
			"Payroll Settings",
			None,
			(
				"payroll_based_on",
				"include_holidays_in_total_working_days",
				"consider_marked_attendance_on_holidays",
				"daily_wages_fraction_for_half_day",
				"consider_unmarked_attendance_as",
			),
			as_dict=1,
		)

		consider_marked_attendance_on_holidays = (
			payroll_settings.include_holidays_in_total_working_days
			and payroll_settings.consider_marked_attendance_on_holidays
		)

		daily_wages_fraction_for_half_day = flt(payroll_settings.daily_wages_fraction_for_half_day) or 0.5

		working_days = date_diff(self.end_date, self.start_date) + 1
		if for_preview:
			self.total_working_days = working_days
			self.payment_days = working_days
			return

		holidays = self.get_holidays_for_employee(self.start_date, self.end_date)
		working_days_list = [add_days(getdate(self.start_date), days=day) for day in range(0, working_days)]

		if not cint(payroll_settings.include_holidays_in_total_working_days):
			working_days_list = [i for i in working_days_list if i not in holidays]

			working_days -= len(holidays)
			if working_days < 0:
				frappe.throw(_("There are more holidays than working days this month."))

		if not payroll_settings.payroll_based_on:
			frappe.throw(_("Please set Payroll based on in Payroll settings"))

		if payroll_settings.payroll_based_on == "Attendance":
			actual_lwp, absent = self.calculate_lwp_ppl_and_absent_days_based_on_attendance(
				holidays, daily_wages_fraction_for_half_day, consider_marked_attendance_on_holidays
			)
			self.absent_days = absent
		else:
			actual_lwp = self.calculate_lwp_or_ppl_based_on_leave_application(
				holidays, working_days_list, daily_wages_fraction_for_half_day
			)

		if not lwp:
			lwp = actual_lwp
		elif lwp != actual_lwp:
			frappe.msgprint(
				_("Leave Without Pay does not match with approved {} records").format(
					payroll_settings.payroll_based_on
				)
			)

		self.leave_without_pay = lwp
		self.total_working_days = working_days

		payment_days = self.get_payment_days(payroll_settings.include_holidays_in_total_working_days)

		if flt(payment_days) > flt(lwp):
			self.payment_days = flt(payment_days) - flt(lwp)

			if payroll_settings.payroll_based_on == "Attendance":
				self.payment_days -= flt(absent)

			consider_unmarked_attendance_as = payroll_settings.consider_unmarked_attendance_as or "Present"

			if (
				payroll_settings.payroll_based_on == "Attendance"
				and consider_unmarked_attendance_as == "Absent"
			):
				unmarked_days = self.get_unmarked_days(
					payroll_settings.include_holidays_in_total_working_days, holidays
				)
				half_absent_days = self.get_half_absent_days(
					payroll_settings.include_holidays_in_total_working_days,
					consider_marked_attendance_on_holidays,
					holidays,
				)
				self.absent_days += (
					unmarked_days + half_absent_days * daily_wages_fraction_for_half_day
				)  # will be treated as absent
				self.payment_days -= unmarked_days + half_absent_days * daily_wages_fraction_for_half_day
		else:
			self.payment_days = 0

def calculate_tax_by_tax_slab(annual_taxable_earning, tax_slab, eval_globals=None, eval_locals=None):
	from hrms.hr.utils import calculate_tax_with_marginal_relief

	frappe.msgprint("This is a placeholder message for debugging purposes. calculate_tax_by_tax_slab function called.")

	tax_amount = 5
	total_other_taxes_and_charges = 5

	return tax_amount, total_other_taxes_and_charges