import frappe
from hrms.payroll.doctype.salary_slip import salary_slip
from custom_hrms.hrms_pakistan.cal_tax import custom_calculate_tax_by_tax_slab

def patch_calculate_tax_by_tax_slab():
    # Replace the original function with the custom one
    salary_slip.calculate_tax_by_tax_slab = custom_calculate_tax_by_tax_slab
    frappe.log_error("Patched calculate_tax_by_tax_slab with custom function", "Patch Debug")