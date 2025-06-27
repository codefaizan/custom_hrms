import frappe
from frappe.utils import cstr, flt

def custom_calculate_tax_by_tax_slab(annual_taxable_earning, tax_slab, eval_globals=None, eval_locals=None):
    from hrms.hr.utils import calculate_tax_with_marginal_relief, eval_tax_slab_condition

    # Add msgprint to verify the function is called
    frappe.msgprint("Custom calculate_tax_by_tax_slab function is running in custom_hrms!")

    tax_amount = 0
    total_other_taxes_and_charges = 0

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
            # tax_amount += other_taxes_and_charges
            tax_amount = 0
            total_other_taxes_and_charges = 0
            # total_other_taxes_and_charges += other_taxes_and_charges

    return tax_amount, total_other_taxes_and_charges