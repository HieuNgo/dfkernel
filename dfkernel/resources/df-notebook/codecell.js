define([
    'jquery',
    'base/js/namespace',
    'base/js/utils',
    'base/js/keyboard',
    'services/config',
    'notebook/js/cell',
    'notebook/js/outputarea',
    'notebook/js/completer',
    'notebook/js/celltoolbar',
    'codemirror/lib/codemirror',
    'codemirror/mode/python/python',
    'notebook/js/codemirror-ipython'
], function(
    $,
    IPython,
    utils,
    keyboard,
    configmod,
    cell,
    outputarea,
    completer,
    celltoolbar,
    CodeMirror,
    cmpython,
    cmip
    ) {
	
	
	var CodeCell = function (kernel, options) {
        /**
         * Constructor
         *
         * A Cell conceived to write code.
         *
         * Parameters:
         *  kernel: Kernel instance
         *      The kernel doesn't have to be set at creation time, in that case
         *      it will be null and set_kernel has to be called later.
         *  options: dictionary
         *      Dictionary of keyword arguments.
         *          events: $(Events) instance
         *          config: dictionary
         *          keyboard_manager: KeyboardManager instance
         *          notebook: Notebook instance
         *          tooltip: Tooltip instance
         */
        this.kernel = kernel || null;
        this.notebook = options.notebook;
        this.collapsed = false;
        this.events = options.events;
        this.tooltip = options.tooltip;
        this.config = options.config;
        this.class_config = new configmod.ConfigWithDefaults(this.config,
                                        CodeCell.options_default, 'CodeCell');

        // create all attributed in constructor function
        // even if null for V8 VM optimisation
        this.input_prompt_number = null;
        this.celltoolbar = null;
        this.output_area = null;
        this.cell_info_area = null;
        this.cell_upstream_deps = null;
        this.cell_downstream_deps = null;

        this.last_msg_id = null;
        this.completer = null;

        Cell.apply(this,[{
            config: options.config,
            keyboard_manager: options.keyboard_manager,
            events: this.events}]);

        // Attributes we want to override in this subclass.
        this.cell_type = "code";

        // create uuid
        // from http://guid.us/GUID/JavaScript
        function S4() {
            return (((1+Math.random())*0x10000)|0).toString(16).substring(1);
        }
        // TODO fix this, just shortening for ease of testing
        //this.uuid = (S4() + S4() + S4() + "4" + S4().substr(0,3) + S4() + S4() + S4() + S4()).toLowerCase();
        this.uuid = S4() + S4().substr(0,2);
        this.was_changed = true;

        var that  = this;
        this.element.focusout(
            function() { that.auto_highlight(); }
        );
    };
	
	/** @method create_element */
    CodeCell.prototype.create_element = function () {
        Cell.prototype.create_element.apply(this, arguments);
        var that = this;

        var cell =  $('<div></div>').addClass('cell code_cell');
        cell.attr('tabindex','2');

        var input = $('<div></div>').addClass('input');
        this.input = input;
        var prompt = $('<div/>').addClass('prompt input_prompt');
        var inner_cell = $('<div/>').addClass('inner_cell');
        this.celltoolbar = new celltoolbar.CellToolbar({
            cell: this,
            notebook: this.notebook});
        inner_cell.append(this.celltoolbar.element);
        var input_area = $('<div/>').addClass('input_area');
        this.code_mirror = new CodeMirror(input_area.get(0), this._options.cm_config);
        // In case of bugs that put the keyboard manager into an inconsistent state,
        // ensure KM is enabled when CodeMirror is focused:
        this.code_mirror.on('focus', function () {
            if (that.keyboard_manager) {
                that.keyboard_manager.enable();
            }

            that.code_mirror.setOption('readOnly', !that.is_editable());
        });
        this.code_mirror.on('keydown', $.proxy(this.handle_keyevent,this));
        this.code_mirror.on('change', function() {
            that.was_changed = true;
        })
        $(this.code_mirror.getInputField()).attr("spellcheck", "false");
        inner_cell.append(input_area);
        input.append(prompt).append(inner_cell);

        var output = $('<div></div>');

        var info = $('<div></div>').addClass("cellinfo");
        var downstream_h = $('<h5>Downstream Dependencies </h5>').addClass('downstream-deps');
        var downstream_button = $('<span/>').addClass("ui-button ui-icon ui-icon-triangle-1-e");
        downstream_h.prepend(downstream_button);
        var select_downstream = $('<a>Select All</a>');
        var update_downstream = $('<a>Update All</a>');
        downstream_h.append(select_downstream)
        downstream_h.append("&nbsp;");
        downstream_h.append(update_downstream);
        var downstream_list = $('<ul></ul>');
        info.append(downstream_h);
        info.append(downstream_list);
        update_downstream.click(function() {
            var cids = $('li a', downstream_list).map(function() { return $(this).attr('href').substring(1); }).get();
            that.notebook.execute_cells_by_id(cids);
            that.notebook.select_cells_by_id(cids);
        });
        select_downstream.click(function() {
            var cids = $('li a', downstream_list).map(function() { return $(this).attr('href').substring(1); }).get();
            that.notebook.select_cells_by_id(cids);
        });

        var upstream_h = $('<h5>Upstream Dependencies </h5>').addClass('upstream-deps');
        var upstream_button = $('<span/>').addClass("ui-button ui-icon ui-icon-triangle-1-e");
        upstream_h.prepend(upstream_button);
        var select_upstream = $('<a>Select All</a>');
        upstream_h.append(select_upstream);

        var upstream_list = $('<ul></ul>');
        info.append(upstream_h);
        info.append(upstream_list);

        select_upstream.click(function() {
            var cids = $('li a', upstream_list).map(function() { return $(this).attr('href').substring(1); }).get();
            that.notebook.select_cells_by_id(cids);
        });


	    info.children('h5').click(function() {
	        $(this).children('.ui-icon').toggleClass("ui-icon-triangle-1-e ui-icon-triangle-1-s");
		    $(this).next().toggle();
		    return false;
	    }).next().hide();

	    $('.upstream-deps', info).hide();
	    $('.downstream-deps', info).hide();

	    this.cell_info_area = info;
	    this.cell_upstream_deps = upstream_list;
	    this.cell_downstream_deps = downstream_list;

        //info.hide();
        cell.append(input).append(output).append(info);
        this.element = cell;
        this.element.attr('id', this.uuid);
        var aname = $('<a/>');
        aname.attr('name', this.uuid);
        this.element.append(aname);
        this.output_area = new outputarea.OutputArea({
            config: this.config,
            selector: output,
            prompt_area: true,
            events: this.events,
            keyboard_manager: this.keyboard_manager,
        });
        this.completer = new completer.Completer(this, this.events);
    };

    CodeCell.prototype.execute = function (stop_on_error) {
        if (!this.kernel) {
            console.log("Can't execute cell since kernel is not set.");
            return;
        }

        if (stop_on_error === undefined) {
            stop_on_error = true;
        }

        // this.output_area.clear_output(false, true);
        this.clear_output(false, true);
        var old_msg_id = this.last_msg_id;
        if (old_msg_id) {
            this.kernel.clear_callbacks_for_msg(old_msg_id);
            delete CodeCell.msg_cells[old_msg_id];
            this.last_msg_id = null;
        }
        if (this.get_text().trim().length === 0) {
            // nothing to do
            this.set_input_prompt(null);
            return;
        }
        this.set_input_prompt('*');
        this.element.addClass("running");

        this.notebook.last_executed_iii = this.notebook.last_executed_ii;
        this.notebook.last_executed_ii = this.notebook.last_executed_i;
        this.notebook.last_executed_i = this.uuid;

        var callbacks = this.get_callbacks();

        this.last_msg_id = this.kernel.execute(this.get_text(), callbacks, {silent: false, store_history: true,
            stop_on_error : stop_on_error, user_expressions: {'__uuid__': this.uuid,
                '__code_dict__': this.notebook.get_code_dict()} });
        CodeCell.msg_cells[this.last_msg_id] = this;
        this.render();
        this.events.trigger('execute.CodeCell', {cell: this});
        var that = this;
        function handleFinished(evt, data) {
            if (that.kernel.id === data.kernel.id && that.last_msg_id === data.msg_id) {
            		that.events.trigger('finished_execute.CodeCell', {cell: that});
                that.events.off('finished_iopub.Kernel', handleFinished);
      	    }
        }
        this.events.on('finished_iopub.Kernel', handleFinished);
    };

     /**
     * Construct the default callbacks for
     * @method get_callbacks
     */
    CodeCell.prototype.get_callbacks = function () {
        var that = this;
        return {
            clear_on_done: false,
            shell : {
                reply : $.proxy(this._handle_execute_reply, this),
                payload : {
                    set_next_input : $.proxy(this._handle_set_next_input, this),
                    page : $.proxy(this._open_with_pager, this)
                }
            },
            iopub : {
                output : function() {
                    that.events.trigger('set_dirty.Notebook', {value: true});
                    var cell = null;
                    console.log("GOT iopub output msg", arguments[0]);
                    if (arguments[0].content.execution_count !== undefined) {
                        cell = that.notebook.get_code_cell(arguments[0].content.execution_count);
                    }
                    if (!cell) {
                        cell = that;
                    }
                    cell.output_area.handle_output.apply(cell.output_area, arguments);
                },
                clear_output : function() {
                    that.events.trigger('set_dirty.Notebook', {value: true});
                    that.output_area.handle_clear_output.apply(that.output_area, arguments);
                },
                execute_input : function() {
                    var cid = arguments[0].content.execution_count;
                    // console.log("CID:", cid);
                    var cell = that.notebook.get_code_cell(cid);
                    if (cell) {
                        // cell.output_area.clear_output(false, true);
                        cell.clear_output(false, true);
                        cell.set_input_prompt('*');
                        cell.element.addClass("running");
                        cell.render();
                        this.events.trigger('execute.CodeCell', {cell: cell});
                    }
                }
            },
            input : $.proxy(this._handle_input_request, this),
        };
    };

        /**
     * @method _handle_execute_reply
     * @private
     */
    CodeCell.prototype._handle_execute_reply = function (msg) {
        // console.log('EXCUTE_REPLY', msg);
        var cell = this.notebook.get_code_cell(msg.content.execution_count);
        if (!cell) {
            cell = this;
        }
        cell.set_input_prompt(msg.content.execution_count);
        cell.element.removeClass("running");
        if (cell == this) {
            var that = this;
            msg.content.upstream_deps.forEach(function (cid) {
                var new_item = $('<li></li>');
                var new_ahref = $('<a></a>');
                new_ahref.attr('href', '#' + cid);
                new_ahref.text("Cell[" + cid + "]");
                new_ahref.click(function () {
                    that.notebook.select_by_id(cid);
                    return false;
                })
                new_item.append(new_ahref);
                that.cell_upstream_deps.append(new_item);
                $('.upstream-deps', that.cell_info_area).show();
            });
            msg.content.downstream_deps.forEach(function (cid) {
                var new_item = $('<li></li>');
                var new_ahref = $('<a></a>');
                new_ahref.attr('href', '#' + cid);
                new_ahref.text("Cell[" + cid + "]");
                new_ahref.click(function () {
                    that.notebook.select_by_id(cid);
                    return false;
                })
                new_item.append(new_ahref);
                that.cell_downstream_deps.append(new_item);
                $('.downstream-deps', that.cell_info_area).show();
            });
        }
        cell.events.trigger('set_dirty.Notebook', {value: true});
    };

    CodeCell.prototype.set_input_prompt = function (number) {
        var nline = 1;
        if (this.code_mirror !== undefined) {
           nline = this.code_mirror.lineCount();
        }
        if (number != '*') {
            number = this.uuid;
        }
        this.input_prompt_number = number;
        var prompt_html = CodeCell.input_prompt_function(number, nline);
        // This HTML call is okay because the user contents are escaped.
        this.element.find('div.input_prompt').html(prompt_html);
        this.events.trigger('set_dirty.Notebook', {value: true});
    };

        CodeCell.prototype.clear_output = function (wait, ignore_queue) {
        this.events.trigger('clear_output.CodeCell', {cell: this});
        this.output_area.clear_output(wait, ignore_queue);
        this.set_input_prompt();
        $('.upstream-deps', this.cell_info_area).hide();
        $('.downstream-deps', this.cell_info_area).hide();
        $('.ui-icon', this.cell_info_area).removeClass('ui-icon-triangle-1-s').addClass('ui-icon-triangle-1-e');
        $(this.cell_upstream_deps).empty();
        $(this.cell_upstream_deps).hide();
        $(this.cell_downstream_deps).empty();
        $(this.cell_downstream_deps).hide();
    };

        // JSON serialization

    CodeCell.prototype.fromJSON = function (data) {
        Cell.prototype.fromJSON.apply(this, arguments);
        if (data.cell_type === 'code') {
            if (data.source !== undefined) {
                this.set_text(data.source);
                // make this value the starting point, so that we can only undo
                // to this state, instead of a blank cell
                this.code_mirror.clearHistory();
                this.auto_highlight();
            }
            this.uuid = (data.execution_count).toString(16);
            this.element.attr('id', this.uuid);
            var aname = $('<a/>');
            aname.attr('name', this.uuid);
            this.element.append(aname);
            if(data.outputs.length > 0) {
                data.outputs[0].uuid = this.uuid;
                //this.output_area.uuid = this.uuid;
            }
            // this.set_input_prompt(data.execution_count);
            this.set_input_prompt();
            this.output_area.trusted = data.metadata.trusted || false;
            this.output_area.fromJSON(data.outputs, data.metadata);
            this.was_changed = true;
        }
    };

    CodeCell.prototype.toJSON = function () {
        var data = Cell.prototype.toJSON.apply(this);
        data.source = this.get_text();
        // is finite protect against undefined and '*' value
        // if (isFinite(this.input_prompt_number)) {
        //     data.execution_count = this.input_prompt_number;
        // } else {
        //     data.execution_count = null;
        // }
        data.execution_count = parseInt(this.uuid,16);
        delete data.uuid;
        //data.uuid = "none";

        var outputs = this.output_area.toJSON();
        console.log(outputs.length);
        if(outputs.length > 0)
        {
            console.log(outputs[0].uuid)
            console.log(outputs[0].execution_count)
            //outputs[0].uuid = "none";
            delete outputs[0].uuid;
            if(outputs.output_type === "execute_result"){
                outputs[0].execution_count = data.execution_count;
            }
        }
        data.outputs = outputs
        data.metadata.trusted = this.output_area.trusted;
        if (this.output_area.collapsed) {
            data.metadata.collapsed = this.output_area.collapsed;
        } else {
            delete data.metadata.collapsed;
        }
        if (this.output_area.scroll_state === 'auto') {
            delete data.metadata.scrolled;
        } else {
            data.metadata.scrolled = this.output_area.scroll_state;
        }
        return data;
    };


		
	});