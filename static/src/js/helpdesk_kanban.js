odoo.define('bmi_invoice_parser.helpdesk_kanban', function (require) {
    "use strict";

    const KanbanController = require('web.KanbanController');
    const KanbanView = require('web.KanbanView');
    const viewRegistry = require('web.view_registry');
    const core = require('web.core');
    const _t = core._t;
    const ajax = require('web.ajax');
    const session = require('web.session');

    const HelpdeskKanbanController = KanbanController.extend({
        /**
         * @override
         */
        renderButtons: function () {
            this._super.apply(this, arguments);
            if (this.$buttons) {
                this.$processButton = $('<button/>', {
                    text: _t('Procesar Primeros 10'),
                    class: 'btn btn-primary o_process_tickets_button',
                });
                this.$processButton.on('click', this._onProcessFirstTickets.bind(this));
                this.$buttons.append(this.$processButton);
            }
        },

        /**
         * Handler when clicking on 'Process First Tickets' button
         * @private
         */
        _onProcessFirstTickets: function () {
            const self = this;
            // Show loading indicator
            this.$processButton.text(_t('Procesando...'));
            this.$processButton.attr('disabled', 'disabled');

            // Get the first 10 tickets with status "Facturas nuevas"
            this._rpc({
                model: 'helpdesk.stage',
                method: 'search',
                args: [[['name', '=', 'Facturas nuevas']]],
                limit: 1,
            }).then(function(stageIds) {
                if (!stageIds || stageIds.length === 0) {
                    self.displayNotification({
                        title: _t('Error'),
                        message: _t('No se encontró la etapa "Facturas nuevas"'),
                        type: 'danger',
                    });
                    self.$processButton.text(_t('Procesar Primeros 10'));
                    self.$processButton.removeAttr('disabled');
                    return;
                }

                // Get tickets with that stage
                return self._rpc({
                    model: 'helpdesk.ticket',
                    method: 'search',
                    args: [[['stage_id', '=', stageIds[0]]]],
                    limit: 10,
                });
            }).then(function(ticketIds) {
                if (!ticketIds || ticketIds.length === 0) {
                    self.displayNotification({
                        title: _t('Información'),
                        message: _t('No hay tickets con estado "Facturas nuevas" para procesar'),
                        type: 'info',
                    });
                    self.$processButton.text(_t('Procesar Primeros 10'));
                    self.$processButton.removeAttr('disabled');
                    return;
                }

                // Process those tickets
                return self._rpc({
                    model: 'helpdesk.ticket',
                    method: 'procesar_facturas',
                    args: [ticketIds],
                }).then(function() {
                    self.displayNotification({
                        title: _t('Éxito'),
                        message: _t('Se procesaron ' + ticketIds.length + ' tickets correctamente'),
                        type: 'success',
                    });
                    // Reload the view
                    self.reload();
                });
            }).catch(function(error) {
                console.error("Error processing tickets:", error);
                self.displayNotification({
                    title: _t('Error'),
                    message: _t('Ocurrió un error al procesar los tickets'),
                    type: 'danger',
                });
            }).finally(function() {
                self.$processButton.text(_t('Procesar Primeros 10'));
                self.$processButton.removeAttr('disabled');
            });
        }
    });

    const HelpdeskKanbanView = KanbanView.extend({
        config: _.extend({}, KanbanView.prototype.config, {
            Controller: HelpdeskKanbanController,
        }),
    });

    viewRegistry.add('helpdesk_kanban_bmi', HelpdeskKanbanView);

    return HelpdeskKanbanView;
});