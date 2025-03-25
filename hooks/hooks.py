import logging

_logger = logging.getLogger(__name__)


def post_init_hook(cr, registry):
    """
    Post-init hook to ensure required helpdesk stages exist.
    This runs after module installation, avoiding XML data issues.
    """
    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})

    # Create or update required stages
    stages = [
        ('Facturas Nuevas', 1),
        ('Tickets sin PDF', 2),
        ('PDF sin PO#', 3),
        ('PO# Inexistente', 4)
    ]

    for name, seq in stages:
        stage = env['helpdesk.stage'].search([('name', '=', name)], limit=1)
        if not stage:
            _logger.info(f"Creating helpdesk stage: {name}")
            env['helpdesk.stage'].create({
                'name': name,
                'sequence': seq
            })
        else:
            _logger.info(f"Helpdesk stage already exists: {name}")

    # Create XML IDs for these stages
    for name, _ in stages:
        stage = env['helpdesk.stage'].search([('name', '=', name)], limit=1)
        if stage:
            xml_id = f"bmi_invoice_parser.stage_{'_'.join(name.lower().split())}"
            # Check if this XML ID already exists
            if not env['ir.model.data'].search([
                ('model', '=', 'helpdesk.stage'),
                ('res_id', '=', stage.id),
                ('module', '=', 'bmi_invoice_parser')
            ]):
                # Create the XML ID manually
                env['ir.model.data'].create({
                    'name': f"stage_{'_'.join(name.lower().split())}",
                    'model': 'helpdesk.stage',
                    'res_id': stage.id,
                    'module': 'bmi_invoice_parser',
                    'noupdate': True
                })
                _logger.info(f"Created XML ID {xml_id} for stage {name}")


def uninstall_hook(cr, registry):
    """
    Clean up any data created by this module.
    """
    pass