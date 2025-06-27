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
	def setup_init(self, *args, **kwargs):
		super().setup_init(*args, **kwargs)
		self.series = f"Sal Slip/{self.employee}/.#####"
		self.whitelisted_globals = {
			"int": int,
			"float": float,
			"long": int,
			"round": round,
			"rounded": rounded,
			"date": date,
			"getdate": getdate,
			"get_first_day": get_first_day,
			"get_last_day": get_last_day,
			"ceil": ceil,
			"floor": floor,
		}

	# def autoname(self):
	# 	self.name = make_autoname(self.series)

	def calculate_variable_tax(self, tax_component):
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

	def get_income_tax_slabs(self):
		income_tax_slab = self.salary_structure_assignment.income_tax_slab

		if not income_tax_slab:
			frappe.throw(
				_("Income Tax Slab not set in Salary Structure Assignment: {0}").format(
					get_link_to_form("Salary Structure Assignment", self.salary_structure_assignment.name)
				),
				title=_("Missing Tax Slab"),
			)

		income_tax_slab_doc = frappe.get_cached_doc("Income Tax Slab", income_tax_slab)
		if income_tax_slab_doc.disabled:
			frappe.throw(_("Income Tax Slab: {0} is disabled").format(income_tax_slab))

		if getdate(income_tax_slab_doc.effective_from) > getdate(self.payroll_period.start_date):
			frappe.throw(
				_("Income Tax Slab must be effective on or before Payroll Period Start Date: {0}").format(
					self.payroll_period.start_date
				)
			)

		return income_tax_slab_doc

	def get_taxable_earnings_for_prev_period(self, start_date, end_date, allow_tax_exemption=False):
		exempted_amount = 0
		taxable_earnings = self.get_salary_slip_details(
			start_date, end_date, parentfield="earnings", is_tax_applicable=1
		)

		if allow_tax_exemption:
			exempted_amount = self.get_salary_slip_details(
				start_date, end_date, parentfield="deductions", exempted_from_income_tax=1
			)

		opening_taxable_earning = self.get_opening_for("taxable_earnings_till_date", start_date, end_date)

		return (taxable_earnings + opening_taxable_earning) - exempted_amount, exempted_amount

	def get_opening_for(self, field_to_select, start_date, end_date):
		if self.salary_structure_assignment.from_date < self.payroll_period.start_date:
			return 0
		return self.salary_structure_assignment.get(field_to_select) or 0

	def get_salary_slip_details(
		self,
		start_date,
		end_date,
		parentfield,
		salary_component=None,
		is_tax_applicable=None,
		is_flexible_benefit=0,
		exempted_from_income_tax=0,
		variable_based_on_taxable_salary=0,
		field_to_select="amount",
	):
		ss = frappe.qb.DocType("Salary Slip")
		sd = frappe.qb.DocType("Salary Detail")

		if field_to_select == "amount":
			field = sd.amount
		else:
			field = sd.additional_amount

		query = (
			frappe.qb.from_(ss)
			.join(sd)
			.on(sd.parent == ss.name)
			.select(Sum(field))
			.where(sd.parentfield == parentfield)
			.where(sd.is_flexible_benefit == is_flexible_benefit)
			.where(ss.docstatus == 1)
			.where(ss.employee == self.employee)
			.where(ss.start_date.between(start_date, end_date))
			.where(ss.end_date.between(start_date, end_date))
		)

		if is_tax_applicable is not None:
			query = query.where(sd.is_tax_applicable == is_tax_applicable)

		if exempted_from_income_tax:
			query = query.where(sd.exempted_from_income_tax == exempted_from_income_tax)

		if variable_based_on_taxable_salary:
			query = query.where(sd.variable_based_on_taxable_salary == variable_based_on_taxable_salary)

		if salary_component:
			query = query.where(sd.salary_component == salary_component)

		result = query.run()

		return flt(result[0][0]) if result else 0.0

	def get_tax_paid_in_period(self, start_date, end_date, tax_component):
		# find total_tax_paid, tax paid for benefit, additional_salary
		total_tax_paid = self.get_salary_slip_details(
			start_date,
			end_date,
			parentfield="deductions",
			salary_component=tax_component,
			variable_based_on_taxable_salary=1,
		)

		tax_deducted_till_date = self.get_opening_for("tax_deducted_till_date", start_date, end_date)

		return total_tax_paid + tax_deducted_till_date

	def get_taxable_earnings(self, allow_tax_exemption=False, based_on_payment_days=0):
		taxable_earnings = 0
		additional_income = 0
		additional_income_with_full_tax = 0
		flexi_benefits = 0
		amount_exempted_from_income_tax = 0

		for earning in self.earnings:
			if based_on_payment_days:
				amount, additional_amount = self.get_amount_based_on_payment_days(earning)
			else:
				if earning.additional_amount:
					amount, additional_amount = earning.amount, earning.additional_amount
				else:
					amount, additional_amount = earning.default_amount, earning.additional_amount

			if earning.is_tax_applicable:
				if earning.is_flexible_benefit:
					flexi_benefits = flexi_benefits + amount
				else:
					taxable_earnings = taxable_earnings + amount - additional_amount
					additional_income = additional_income + additional_amount

					# Get additional amount based on future recurring additional salary
					if additional_amount and earning.is_recurring_additional_salary:
						additional_income = additional_income + self.get_future_recurring_additional_amount(
							earning.additional_salary, earning.additional_amount
						)  # Used earning.additional_amount to consider the amount for the full month

					if earning.deduct_full_tax_on_selected_payroll_date:
						additional_income_with_full_tax = additional_income_with_full_tax + additional_amount

		if allow_tax_exemption:
			for ded in self.deductions:
				if ded.exempted_from_income_tax:
					amount, additional_amount = ded.amount, ded.additional_amount
					if based_on_payment_days:
						amount, additional_amount = self.get_amount_based_on_payment_days(ded)

					taxable_earnings = taxable_earnings - flt(amount - additional_amount)
					additional_income = additional_income - additional_amount
					amount_exempted_from_income_tax = amount_exempted_from_income_tax + flt(amount - additional_amount)

					if additional_amount and ded.is_recurring_additional_salary:
						additional_income = additional_income - self.get_future_recurring_additional_amount(
							ded.additional_salary, ded.additional_amount
						)  # Used ded.additional_amount to consider the amount for the full month

		return frappe._dict(
			{
				"taxable_earnings": taxable_earnings,
				"additional_income": additional_income,
				"amount_exempted_from_income_tax": amount_exempted_from_income_tax,
				"additional_income_with_full_tax": additional_income_with_full_tax,
				"flexi_benefits": flexi_benefits,
			}
		)

	def get_future_recurring_period(
		self,
		additional_salary,
	):
		to_date = None

		if self.relieving_date:
			to_date = self.relieving_date

		if not to_date:
			to_date = frappe.db.get_value("Additional Salary", additional_salary, "to_date", cache=True)

		# future month count excluding current
		from_date, to_date = getdate(self.start_date), getdate(to_date)

		# If recurring period end date is beyond the payroll period,
		# last day of payroll period should be considered for recurring period calculation
		if getdate(to_date) > getdate(self.payroll_period.end_date):
			to_date = getdate(self.payroll_period.end_date)

		future_recurring_period = ((to_date.year - from_date.year) * 12) + (to_date.month - from_date.month)

		if future_recurring_period > 0 and to_date.month == from_date.month:
			future_recurring_period -= 1

		return future_recurring_period

	def get_future_recurring_additional_amount(self, additional_salary, monthly_additional_amount):
		future_recurring_additional_amount = 0

		future_recurring_period = self.get_future_recurring_period(additional_salary)

		if future_recurring_period > 0:
			future_recurring_additional_amount = (
				monthly_additional_amount * future_recurring_period
			)  # Used earning.additional_amount to consider the amount for the full month
		return future_recurring_additional_amount

	def get_amount_based_on_payment_days(self, row):
		amount, additional_amount = row.amount, row.additional_amount
		timesheet_component = self.salary_structure_doc.salary_component

		if (
			self.salary_structure
			and cint(row.depends_on_payment_days)
			and cint(self.total_working_days)
			and not (
				row.additional_salary and row.default_amount
			)  # to identify overwritten additional salary
			and (
				row.salary_component != timesheet_component
				or getdate(self.start_date) < self.joining_date
				or (self.relieving_date and getdate(self.end_date) > self.relieving_date)
			)
		):
			additional_amount = flt(
				(flt(row.additional_amount) * flt(self.payment_days) / cint(self.total_working_days)),
				row.precision("additional_amount"),
			)
			amount = (
				flt(
					(flt(row.default_amount) * flt(self.payment_days) / cint(self.total_working_days)),
					row.precision("amount"),
				)
				+ additional_amount
			)

		elif (
			not self.payment_days
			and row.salary_component != timesheet_component
			and cint(row.depends_on_payment_days)
		):
			amount, additional_amount = 0, 0
		elif not row.amount:
			amount = flt(row.default_amount) + flt(row.additional_amount)

		# apply rounding
		if frappe.db.get_value(
			"Salary Component", row.salary_component, "round_to_the_nearest_integer", cache=True
		):
			amount, additional_amount = rounded(amount or 0), rounded(additional_amount or 0)

		return amount, additional_amount

	def calculate_unclaimed_taxable_benefits(self):
		# get total sum of benefits paid
		total_benefits_paid = self.get_salary_slip_details(
			self.payroll_period.start_date,
			self.start_date,
			parentfield="earnings",
			is_tax_applicable=1,
			is_flexible_benefit=1,
		)

		# get total benefits claimed
		BenefitClaim = frappe.qb.DocType("Employee Benefit Claim")
		total_benefits_claimed = (
			frappe.qb.from_(BenefitClaim)
			.select(Sum(BenefitClaim.claimed_amount))
			.where(
				(BenefitClaim.docstatus == 1)
				& (BenefitClaim.employee == self.employee)
				& (BenefitClaim.claim_date.between(self.payroll_period.start_date, self.end_date))
			)
		).run()
		total_benefits_claimed = flt(total_benefits_claimed[0][0]) if total_benefits_claimed else 0

		unclaimed_taxable_benefits = (
			total_benefits_paid - total_benefits_claimed
		) + self.current_taxable_earnings_for_payment_days.flexi_benefits
		return unclaimed_taxable_benefits

	def get_total_exemption_amount(self):
		total_exemption_amount = 0
		if self.tax_slab.allow_tax_exemption:
			if self.deduct_tax_for_unsubmitted_tax_exemption_proof:
				exemption_proof = frappe.db.get_value(
					"Employee Tax Exemption Proof Submission",
					{"employee": self.employee, "payroll_period": self.payroll_period.name, "docstatus": 1},
					"exemption_amount",
					cache=True,
				)
				if exemption_proof:
					total_exemption_amount = exemption_proof
			else:
				declaration = frappe.db.get_value(
					"Employee Tax Exemption Declaration",
					{"employee": self.employee, "payroll_period": self.payroll_period.name, "docstatus": 1},
					"total_exemption_amount",
					cache=True,
				)
				if declaration:
					total_exemption_amount = declaration

		if self.tax_slab.standard_tax_exemption_amount:
			total_exemption_amount = total_exemption_amount + flt(self.tax_slab.standard_tax_exemption_amount)

		return total_exemption_amount

	def get_income_form_other_sources(self):
		return (
			frappe.get_all(
				"Employee Other Income",
				filters={
					"employee": self.employee,
					"payroll_period": self.payroll_period.name,
					"company": self.company,
					"docstatus": 1,
				},
				fields="SUM(amount) as total_amount",
			)[0].total_amount
			or 0.0
		)

	def get_component_totals(self, component_type, depends_on_payment_days=0):
		total = 0.0
		for d in self.get(component_type):
			if not d.do_not_include_in_total:
				if depends_on_payment_days:
					amount = self.get_amount_based_on_payment_days(d)[0]
				else:
					amount = flt(d.amount, d.precision("amount"))
				total = total + amount
		return total

	# def email_salary_slip(self):
	# 	receiver = frappe.db.get_value("Employee", self.employee, "prefered_email", cache=True)
	# 	payroll_settings = frappe.get_single("Payroll Settings")

	# 	subject = f"Salary Slip - from {self.start_date} to {self.end_date}"
	# 	message = _("Please see attachment")
	# 	if payroll_settings.email_template:
	# 		email_template = frappe.get_doc("Email Template", payroll_settings.email_template)
	# 		context = self.as_dict()
	# 		subject = frappe.render_template(email_template.subject, context)
	# 		message = frappe.render_template(email_template.response, context)

	# 	password = None
	# 	if payroll_settings.encrypt_salary_slips_in_emails:
	# 		password = generate_password_for_pdf(payroll_settings.password_policy, self.employee)
	# 		if not payroll_settings.email_template:
	# 			message = message + "<br>" + _(
	# 				"Note: Your salary slip is password protected, the password to unlock the PDF is of the format {0}."
	# 			).format(payroll_settings.password_policy)

	# 	if receiver:
	# 		email_args = {
	# 			"sender": payroll_settings.sender_email,
	# 			"recipients": [receiver],
	# 			"message": message,
	# 			"subject": subject,
	# 			"attachments": [
	# 				frappe.attach_print(self.doctype, self.name, file_name=self.name, password=password)
	# 			],
	# 			"reference_doctype": self.doctype,
	# 			"reference_name": self.name,
	# 		}
	# 		if not frappe.flags.in_test:
	# 			enqueue(method=frappe.sendmail, queue="short", timeout=300, is_async=True, **email_args)
	# 		else:
	# 			frappe.sendmail(**email_args)
	# 	else:
	# 		msgprint(_("{0}: Employee email not found, hence email not sent").format(self.employee_name))

	def update_status(self, salary_slip=None):
		for data in self.timesheets:
			if data.time_sheet:
				timesheet = frappe.get_doc("Timesheet", data.time_sheet)
				timesheet.salary_slip = salary_slip
				timesheet.flags.ignore_validate_update_after_submit = True
				timesheet.set_status()
				timesheet.save()

	def set_status(self, status=None):
		"""Get and update status"""
		if not status:
			status = self.get_status()
		self.db_set("status", status)

	def process_salary_structure(self, for_preview=0):
		"""Calculate salary after salary structure details have been updated"""
		if self.payroll_frequency:
			self.get_date_details()
		self.pull_emp_details()
		self.get_working_days_details(for_preview=for_preview)
		self.calculate_net_pay()

	def pull_emp_details(self):
		account_details = frappe.get_cached_value(
			"Employee", self.employee, ["bank_name", "bank_ac_no", "salary_mode"], as_dict=1
		)
		if account_details:
			self.mode_of_payment = account_details.salary_mode
			self.bank_name = account_details.bank_name
			self.bank_account_no = account_details.bank_ac_no

	@frappe.whitelist()
	def process_salary_based_on_working_days(self):
		self.get_working_days_details(lwp=self.leave_without_pay)
		self.calculate_net_pay()

	@frappe.whitelist()
	def set_totals(self):
		self.gross_pay = 0.0
		if self.salary_slip_based_on_timesheet == 1:
			self.calculate_total_for_salary_slip_based_on_timesheet()
		else:
			self.total_deduction = 0.0
			if hasattr(self, "earnings"):
				for earning in self.earnings:
					self.gross_pay = self.gross_pay + flt(earning.amount, earning.precision("amount"))
			if hasattr(self, "deductions"):
				for deduction in self.deductions:
					self.total_deduction = self.total_deduction + flt(deduction.amount, deduction.precision("amount"))
			self.net_pay = (
				flt(self.gross_pay) - flt(self.total_deduction) - flt(self.get("total_loan_repayment"))
			)
		self.set_base_totals()

	def set_base_totals(self):
		self.base_gross_pay = flt(self.gross_pay) * flt(self.exchange_rate)
		self.base_total_deduction = flt(self.total_deduction) * flt(self.exchange_rate)
		self.rounded_total = rounded(self.net_pay or 0)
		self.base_net_pay = flt(self.net_pay) * flt(self.exchange_rate)
		self.base_rounded_total = rounded(self.base_net_pay or 0)
		self.set_net_total_in_words()

	# calculate total working hours, earnings based on hourly wages and totals
	def calculate_total_for_salary_slip_based_on_timesheet(self):
		if self.timesheets:
			self.total_working_hours = 0
			for timesheet in self.timesheets:
				if timesheet.working_hours:
					self.total_working_hours = self.total_working_hours + timesheet.working_hours

		wages_amount = self.total_working_hours * self.hour_rate
		self.base_hour_rate = flt(self.hour_rate) * flt(self.exchange_rate)
		salary_component = frappe.db.get_value(
			"Salary Structure", {"name": self.salary_structure}, "salary_component", cache=True
		)
		if self.earnings:
			for i, earning in enumerate(self.earnings):
				if earning.salary_component == salary_component:
					self.earnings[i].amount = wages_amount
				self.gross_pay = self.gross_pay + flt(self.earnings[i].amount, earning.precision("amount"))
		self.net_pay = flt(self.gross_pay) - flt(self.total_deduction)

	def compute_year_to_date(self):
		year_to_date = 0
		period_start_date, period_end_date = self.get_year_to_date_period()

		salary_slip_sum = frappe.get_list(
			"Salary Slip",
			fields=["sum(net_pay) as net_sum", "sum(gross_pay) as gross_sum"],
			filters={
				"employee": self.employee,
				"start_date": [">=", period_start_date],
				"end_date": ["<", period_end_date],
				"name": ["!=", self.name],
				"docstatus": 1,
			},
		)

		year_to_date = flt(salary_slip_sum[0].net_sum) if salary_slip_sum else 0.0
		gross_year_to_date = flt(salary_slip_sum[0].gross_sum) if salary_slip_sum else 0.0

		year_to_date = year_to_date + self.net_pay
		gross_year_to_date = gross_year_to_date + self.gross_pay
		self.year_to_date = year_to_date
		self.gross_year_to_date = gross_year_to_date

	def compute_month_to_date(self):
		month_to_date = 0
		first_day_of_the_month = get_first_day(self.start_date)
		salary_slip_sum = frappe.get_list(
			"Salary Slip",
			fields=["sum(net_pay) as sum"],
			filters={
				"employee": self.employee,
				"start_date": [">=", first_day_of_the_month],
				"end_date": ["<", self.start_date],
				"name": ["!=", self.name],
				"docstatus": 1,
			},
		)

		month_to_date = flt(salary_slip_sum[0].sum) if salary_slip_sum else 0.0

		month_to_date += self.net_pay
		self.month_to_date = month_to_date

	def compute_component_wise_year_to_date(self):
		period_start_date, period_end_date = self.get_year_to_date_period()

		ss = frappe.qb.DocType("Salary Slip")
		sd = frappe.qb.DocType("Salary Detail")

		for key in ("earnings", "deductions"):
			for component in self.get(key):
				year_to_date = 0
				component_sum = (
					frappe.qb.from_(sd)
					.inner_join(ss)
					.on(sd.parent == ss.name)
					.select(Sum(sd.amount).as_("sum"))
					.where(
						(ss.employee == self.employee)
						& (sd.salary_component == component.salary_component)
						& (ss.start_date >= period_start_date)
						& (ss.end_date < period_end_date)
						& (ss.name != self.name)
						& (ss.docstatus == 1)
					)
				).run()

				year_to_date = flt(component_sum[0][0]) if component_sum else 0.0
				year_to_date += component.amount
				component.year_to_date = year_to_date

	def get_year_to_date_period(self):
		if self.payroll_period:
			period_start_date = self.payroll_period.start_date
			period_end_date = self.payroll_period.end_date
		else:
			# get dates based on fiscal year if no payroll period exists
			fiscal_year = get_fiscal_year(date=self.start_date, company=self.company, as_dict=1)
			period_start_date = fiscal_year.year_start_date
			period_end_date = fiscal_year.year_end_date

		return period_start_date, period_end_date

	def add_leave_balances(self):
		self.set("leave_details", [])

		if frappe.db.get_single_value("Payroll Settings", "show_leave_balances_in_salary_slip"):
			from hrms.hr.doctype.leave_application.leave_application import get_leave_details

			leave_details = get_leave_details(self.employee, self.end_date, True)

			for leave_type, leave_values in leave_details["leave_allocation"].items():
				self.append(
					"leave_details",
					{
						"leave_type": leave_type,
						"total_allocated_leaves": flt(leave_values.get("total_leaves")),
						"expired_leaves": flt(leave_values.get("expired_leaves")),
						"used_leaves": flt(leave_values.get("leaves_taken")),
						"pending_leaves": flt(leave_values.get("leaves_pending_approval")),
						"available_leaves": flt(leave_values.get("remaining_leaves")),
					},
				)


def unlink_ref_doc_from_salary_slip(doc, method=None):
	"""Unlinks accrual Journal Entry from Salary Slips on cancellation"""
	linked_ss = frappe.get_all(
		"Salary Slip", filters={"journal_entry": doc.name, "docstatus": ["<", 2]}, pluck="name"
	)

	if linked_ss:
		for ss in linked_ss:
			ss_doc = frappe.get_doc("Salary Slip", ss)
			frappe.db.set_value("Salary Slip", ss_doc.name, "journal_entry", "")


def generate_password_for_pdf(policy_template, employee):
	employee = frappe.get_cached_doc("Employee", employee)
	return policy_template.format(**employee.as_dict())


def get_salary_component_data(component):
	# get_cached_value doesn't work here due to alias "name as salary_component"
	return frappe.db.get_value(
		"Salary Component",
		component,
		(
			"name as salary_component",
			"depends_on_payment_days",
			"salary_component_abbr as abbr",
			"do_not_include_in_total",
			"is_tax_applicable",
			"is_flexible_benefit",
			"variable_based_on_taxable_salary",
		),
		as_dict=1,
		cache=True,
	)


def get_payroll_payable_account(company, payroll_entry):
	if payroll_entry:
		payroll_payable_account = frappe.db.get_value(
			"Payroll Entry", payroll_entry, "payroll_payable_account", cache=True
		)
	else:
		payroll_payable_account = frappe.db.get_value(
			"Company", company, "default_payroll_payable_account", cache=True
		)

	return payroll_payable_account


def calculate_tax_by_tax_slab(annual_taxable_earning, tax_slab, eval_globals=None, eval_locals=None):
	from hrms.hr.utils import calculate_tax_with_marginal_relief

	tax_amount = 5
	total_other_taxes_and_charges = 5

	return tax_amount, total_other_taxes_and_charges


def eval_tax_slab_condition(condition, eval_globals=None, eval_locals=None):
	if not eval_globals:
		eval_globals = {
			"int": int,
			"float": float,
			"long": int,
			"round": round,
			"date": date,
			"getdate": getdate,
			"get_first_day": get_first_day,
			"get_last_day": get_last_day,
		}

	try:
		condition = condition.strip()
		if condition:
			return frappe.safe_eval(condition, eval_globals, eval_locals)
	except NameError as err:
		frappe.throw(
			_("{0} <br> This error can be due to missing or deleted field.").format(err),
			title=_("Name error"),
		)
	except SyntaxError as err:
		frappe.throw(_("Syntax error in condition: {0} in Income Tax Slab").format(err))
	except Exception as e:
		frappe.throw(_("Error in formula or condition: {0} in Income Tax Slab").format(e))
		raise


def get_lwp_or_ppl_for_date_range(employee, start_date, end_date):
	LeaveApplication = frappe.qb.DocType("Leave Application")
	LeaveType = frappe.qb.DocType("Leave Type")

	leaves = (
		frappe.qb.from_(LeaveApplication)
		.inner_join(LeaveType)
		.on(LeaveType.name == LeaveApplication.leave_type)
		.select(
			LeaveApplication.name,
			LeaveType.is_ppl,
			LeaveType.fraction_of_daily_salary_per_leave,
			LeaveType.include_holiday,
			LeaveApplication.from_date,
			LeaveApplication.to_date,
			LeaveApplication.half_day,
			LeaveApplication.half_day_date,
		)
		.where(
			((LeaveType.is_lwp == 1) | (LeaveType.is_ppl == 1))
			& (LeaveApplication.docstatus == 1)
			& (LeaveApplication.status == "Approved")
			& (LeaveApplication.employee == employee)
			& ((LeaveApplication.salary_slip.isnull()) | (LeaveApplication.salary_slip == ""))
			& ((LeaveApplication.from_date <= end_date) & (LeaveApplication.to_date >= start_date))
		)
	).run(as_dict=True)

	leave_date_mapper = frappe._dict()
	for leave in leaves:
		if leave.from_date == leave.to_date:
			leave_date_mapper[leave.from_date] = leave
		else:
			date_diff = (getdate(leave.to_date) - getdate(leave.from_date)).days
			for i in range(date_diff + 1):
				date = add_days(leave.from_date, i)
				leave_date_mapper[date] = leave

	return leave_date_mapper


@frappe.whitelist()
def make_salary_slip_from_timesheet(source_name, target_doc=None):
	target = frappe.new_doc("Salary Slip")
	set_missing_values(source_name, target)
	target.run_method("get_emp_and_working_day_details")

	return target


def set_missing_values(time_sheet, target):
	doc = frappe.get_doc("Timesheet", time_sheet)
	target.employee = doc.employee
	target.employee_name = doc.employee_name
	target.salary_slip_based_on_timesheet = 1
	target.start_date = doc.start_date
	target.end_date = doc.end_date
	target.posting_date = doc.modified
	target.total_working_hours = doc.total_hours
	target.append("timesheets", {"time_sheet": doc.name, "working_hours": doc.total_hours})


def throw_error_message(row, error, title, description=None):
	data = frappe._dict(
		{
			"doctype": row.parenttype,
			"name": row.parent,
			"doclink": get_link_to_form(row.parenttype, row.parent),
			"row_id": row.idx,
			"error": error,
			"title": title,
			"description": description or "",
		}
	)

	message = _(
		"Error while evaluating the {doctype} {doclink} at row {row_id}. <br><br> <b>Error:</b> {error} <br><br> <b>Hint:</b> {description}"
	).format(**data)

	frappe.throw(message, title=title)


def on_doctype_update():
	frappe.db.add_index("Salary Slip", ["employee", "start_date", "end_date"])


def safe_eval_alt(code: str, eval_globals: dict | None = None, eval_locals: dict | None = None):
	"""Old version of safe_eval from framework.

	Note: current frappe.safe_eval transforms code so if you have nested
	iterations with too much depth then it can hit recursion limit of python.
	There's no workaround for this and people need large formulas in some
	countries so this is alternate implementation for that.

	WARNING: DO NOT use this function anywhere else outside of this file.
	"""
	code = unicodedata.normalize("NFKC", code)

	check_attributes(code)

	whitelisted_globals = {"int": int, "float": float, "long": int, "round": round}
	if not eval_globals:
		eval_globals = {}

	eval_globals["__builtins__"] = {}
	eval_globals.update(whitelisted_globals)
	# Note: eval call has been replaced with frappe.safe_eval
	return frappe.safe_eval(code, eval_globals, eval_locals)


def check_attributes(code: str) -> None:
	# Simple check for unsafe attributes that are commonly blocked
	unsafe_attrs = ["__", "eval", "exec", "compile", "open", "file", "input", "raw_input"]
	
	for attribute in unsafe_attrs:
		if attribute in code:
			raise SyntaxError(f'Illegal rule {frappe.bold(code)}. Cannot use "{attribute}"')


@frappe.whitelist()
def enqueue_email_salary_slips(names) -> None:
	"""enqueue bulk emailing salary slips"""
	import json

	if isinstance(names, str):
		names = json.loads(names)

	frappe.enqueue("hrms.payroll.doctype.salary_slip.salary_slip.email_salary_slips", names=names)
	frappe.msgprint(
		_("Salary slip emails have been enqueued for sending. Check {0} for status.").format(
			f"""<a href='{frappe.utils.get_url_to_list("Email Queue")}' target='blank'>Email Queue</a>"""
		)
	)


def email_salary_slips(names) -> None:
	for name in names:
		salary_slip = frappe.get_doc("Salary Slip", name)
		salary_slip.email_salary_slip()
