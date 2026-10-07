"""Keep official single-file reports private and usable in an opaque iframe."""

import re


def private_html(html):
    # Allure 3.20 adds Google Analytics unconditionally. Remove the known block
    # from our private artifact and download; never loosen outbound CSP for it.
    html, removed = re.subn(
        rb'<script async src="https://www\.googletagmanager\.com/gtag/js\?id=G-LNDJ3J7WT0"></script>\s*<script>\s*window\.dataLayer.*?</script>',
        b"",
        html,
        count=1,
        flags=re.DOTALL,
    )
    if removed != 1:
        raise ValueError("Allure 模板变化，需要重新核对离线报告的隐私设置。")
    # Allure stores UI preferences. A sandbox without allow-same-origin cannot
    # use browser storage; provide ephemeral memory, never platform storage.
    shim = b"""<script>(function(){['localStorage','sessionStorage'].forEach(function(name){
      try { void window[name].length; } catch(e) { var values={};
        Object.defineProperty(window,name,{value:{getItem:function(k){return Object.prototype.hasOwnProperty.call(values,k)?values[k]:null;},
        setItem:function(k,v){values[k]=String(v);},removeItem:function(k){delete values[k];},clear:function(){values={};},
        key:function(i){return Object.keys(values)[i]||null;},get length(){return Object.keys(values).length;}}});
      }
    });})();</script>"""
    if b"<head>" not in html:
        raise ValueError("Allure 报告缺少 head 元素。")
    return html.replace(b"<head>", b"<head>" + shim, 1)
