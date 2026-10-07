import { initializeDateTimePicker } from '../../../../static/js/datetime_picker'
import { dataTableJsonRPC, jsonRPC } from '../../../../static/js/jsonrpc'
import { exportButtons } from '../../../../static/js/datatables_common'
import {
    arrayToDict, escapeHTML,
    updateParamsToSearchTags
} from '../../../../static/js/utils'

function preProcessData (data, callbackF) {
    const runIds = []
    const productIds = []
    data.forEach(function (element) {
        runIds.push(element.id)
        productIds.push(element.build__version__product)
    })

    // get tags for all objects
    const tagsPerRun = {}
    jsonRPC('Tag.filter', { run__in: runIds }, function (tags) {
        tags.forEach(function (element) {
            if (tagsPerRun[element.run] === undefined) {
                tagsPerRun[element.run] = []
            }

            // push only if unique
            if (tagsPerRun[element.run].indexOf(element.name) === -1) {
                tagsPerRun[element.run].push(element.name)
            }
        })

        jsonRPC('Product.filter', { pk__in: productIds }, function (products) {
            products = arrayToDict(products)

            // augment data set with additional info
            data.forEach(function (element) {
                if (element.id in tagsPerRun) {
                    element.tag = tagsPerRun[element.id]
                } else {
                    element.tag = []
                }
                element.product_name = products[element.build__version__product].name
            })

            callbackF({ data }) // renders everything
        })
    })
}

export function pageTestrunsSearchReadyHandler () {
    initializeDateTimePicker('#id_before_start_date')
    initializeDateTimePicker('#id_after_start_date')
    initializeDateTimePicker('#id_before_stop_date')
    initializeDateTimePicker('#id_after_stop_date')
    initializeDateTimePicker('#id_before_planned_start')
    initializeDateTimePicker('#id_after_planned_start')
    initializeDateTimePicker('#id_before_planned_stop')
    initializeDateTimePicker('#id_after_planned_stop')

    const initialFilters = new URLSearchParams(window.location.search)
    const dateFields = ['before_start_date', 'after_start_date', 'before_stop_date', 'after_stop_date',
        'before_planned_start', 'after_planned_start', 'before_planned_stop', 'after_planned_stop']
    dateFields.forEach(function (name) {
        const date = window.moment(initialFilters.get(name) || '', 'YYYY-MM-DD', true)
        if (date.isValid()) $('#id_' + name).data('DateTimePicker').date(date)
    })

    $('#resultsTable').DataTable({
        pageLength: $('#navbar').data('defaultpagesize'),
        ajax: function (data, callbackF, settings) {
            const params = {}
            const folder = $('#run-directory-filter')
            if (folder.val()) params._resource_folder = folder.val()
            if (folder.attr('data-invalid-scope') === '1') params.pk__in = []

            if ($('#id_summary').val()) {
                params.summary__icontains = $('#id_summary').val()
            }

            if ($('#id_after_start_date').val()) {
                params.start_date__gte = $('#id_after_start_date').data('DateTimePicker').date().format('YYYY-MM-DD 00:00:00')
            }

            if ($('#id_before_start_date').val()) {
                params.start_date__lte = $('#id_before_start_date').data('DateTimePicker').date().format('YYYY-MM-DD 23:59:59')
            }

            if ($('#id_after_stop_date').val()) {
                params.stop_date__gte = $('#id_after_stop_date').data('DateTimePicker').date().format('YYYY-MM-DD 00:00:00')
            }

            if ($('#id_before_stop_date').val()) {
                params.stop_date__lte = $('#id_before_stop_date').data('DateTimePicker').date().format('YYYY-MM-DD 23:59:59')
            }

            if ($('#id_after_planned_start').val()) {
                params.planned_start__gte = $('#id_after_planned_start').data('DateTimePicker').date().format('YYYY-MM-DD 00:00:00')
            }

            if ($('#id_before_planned_start').val()) {
                params.planned_start__lte = $('#id_before_planned_start').data('DateTimePicker').date().format('YYYY-MM-DD 23:59:59')
            }

            if ($('#id_after_planned_stop').val()) {
                params.planned_stop__gte = $('#id_after_planned_stop').data('DateTimePicker').date().format('YYYY-MM-DD 00:00:00')
            }

            if ($('#id_before_planned_stop').val()) {
                params.planned_stop__lte = $('#id_before_planned_stop').data('DateTimePicker').date().format('YYYY-MM-DD 23:59:59')
            }

            if ($('#id_plan').val()) {
                params.plan = $('#id_plan').val()
            }

            if ($('#id_product').val()) {
                params.build__version__product = $('#id_product').val()
            };

            if ($('#id_version').val()) {
                params.build__version = $('#id_version').val()
            };

            if ($('#id_build').val()) {
                params.build = $('#id_build').val()
            };

            if ($('#id_manager').val()) {
                params.manager__username__startswith = $('#id_manager').val()
            };

            if ($('#id_default_tester').val()) {
                params.default_tester__username__startswith = $('#id_default_tester').val()
            };

            updateParamsToSearchTags('#id_tag', params)

            params.stop_date__isnull = $('#id_running').val() === '1'

            dataTableJsonRPC('TestRun.filter', params, callbackF, preProcessData)
        },
        columns: [
            { data: 'id' },
            {
                data: null,
                render: function (data, type, full, meta) {
                    let result = '<a href="/runs/' + data.id + '/">' + escapeHTML(data.summary) + '</a>'
                    if (data.stop_date) {
                        result += '<p class="help-block">' + data.stop_date + '</p>'
                    }
                    return result
                }
            },
            {
                data: null,
                render: function (data, type, full, meta) {
                    return '<a href="/plan/' + data.plan + '/">TP-' + data.plan + ': ' + escapeHTML(data.plan__name) + '</a>'
                }
            },
            { data: 'product_name' },
            { data: 'build__version__value' },
            { data: 'build__name' },
            { data: 'start_date' },
            { data: 'stop_date' },
            { data: 'manager__username' },
            { data: 'default_tester__username' },
            { data: 'tag' }
        ],
        dom: 'Biptip',
        buttons: exportButtons,
        language: {
            info: '共 _TOTAL_ 条任务',
            infoEmpty: '暂无执行任务',
            loadingRecords: '<div class="spinner spinner-lg"></div>',
            processing: '<div class="spinner spinner-lg"></div>',
            thousands: '',
            zeroRecords: '没有匹配的执行任务',
            paginate: {first: '首页', previous: '上一页', next: '下一页', last: '末页'}
        },
        order: [[0, 'asc']]
    })

    $('#run-filter-form').on('submit', function (event) {
        event.preventDefault()
        const selected = new URLSearchParams()
        const fields = ['summary', 'plan', 'product', 'version', 'build', 'manager', 'default_tester', 'tag']
        fields.forEach(function (name) { selected.set(name, $('#id_' + name).val() || '') })
        selected.set('running', $('#id_running').val())
        dateFields.forEach(function (name) {
            const date = $('#id_' + name).data('DateTimePicker').date()
            if (date) selected.set(name, date.format('YYYY-MM-DD'))
        })
        if ($('#run-directory-filter').val()) selected.set('folder', $('#run-directory-filter').val())
        window.location.assign(window.location.pathname + '?' + selected.toString())
    })

    let versionRequest = 0
    let buildRequest = 0
    function setOptions (selector, rows, label, valueField) {
        const select = document.querySelector(selector)
        select.replaceChildren(new Option(label, ''))
        rows.forEach(function (row) { select.add(new Option(row[valueField], String(row.id))) })
    }
    $('#id_product').change(function () {
        $('#run-directory-filter').val('').attr('data-invalid-scope', '0')
        $('.run-directory-scope').hide()
        const product = this.value
        const current = ++versionRequest
        ++buildRequest
        setOptions('#id_version', [], '全部版本', 'value')
        setOptions('#id_build', [], '全部构建', 'name')
        if (!product) return
        jsonRPC('Version.filter', { product__in: [product] }, function (rows) {
            if (current === versionRequest && $('#id_product').val() === product) {
                setOptions('#id_version', rows, '全部版本', 'value')
            }
        })
    })
    $('#id_version').change(function () {
        $('#run-directory-filter').attr('data-invalid-scope', '0')
        const version = this.value
        const current = ++buildRequest
        setOptions('#id_build', [], '全部构建', 'name')
        if (!version) return
        jsonRPC('Build.filter', { version__in: [version] }, function (rows) {
            if (current === buildRequest && $('#id_version').val() === version) {
                setOptions('#id_build', rows, '全部构建', 'name')
            }
        })
    })
    $('#id_build').change(function () { $('#run-directory-filter').attr('data-invalid-scope', '0') })


}
