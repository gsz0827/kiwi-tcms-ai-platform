// Grappelli shadows Django's related-object script, which expects grp.jQuery.
// Keep its popup API without loading the full admin-page initialization.
window.grp = window.grp || {};
window.grp.jQuery = window.django.jQuery;
