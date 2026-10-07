import { populateVersion } from '../../../../static/js/utils'

/* Used in mutable.html and clone.html. */
export function pageTestplansMutableReadyHandler () {
    const parentSelect = document.getElementById('id_parent')
    const productSelect = document.getElementById('id_product')
    function filterParentPlans () {
        if (!parentSelect || parentSelect.tagName !== 'SELECT') return
        for (const option of parentSelect.options) {
            const visible = !option.value || option.dataset.product === productSelect.value
            option.hidden = !visible
            option.disabled = !visible
        }
        if (parentSelect.selectedOptions.length && parentSelect.selectedOptions[0].disabled) {
            parentSelect.value = ''
        }
    }

    if ($('#id_version').find('option').length === 0) populateVersion()
    $('#add_id_product').click(function () { return showRelatedObjectPopup(this) })
    $('#add_id_version').click(function () { return showRelatedObjectPopup(this) })
    productSelect.onchange = function () {
        $('#id_product').selectpicker('refresh')
        populateVersion()
        filterParentPlans()
    }
    document.getElementById('id_version').onchange = function () {
        $('#id_version').selectpicker('refresh')
    }
    filterParentPlans()
}
