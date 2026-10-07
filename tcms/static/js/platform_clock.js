(function () {
    'use strict'
    const clock = document.getElementById('clock')
    if (!clock || typeof moment === 'undefined' || !moment.tz) return
    const zone = clock.dataset.timeZone || 'Asia/Shanghai'
    const labels = {'Asia/Shanghai': '北京时间', 'Etc/UTC': 'UTC'}
    function updateClock () {
        const current = moment().tz(zone)
        clock.textContent = current.format('HH:mm') + ' ' + (labels[zone] || zone)
        clock.title = current.format('YYYY-MM-DD HH:mm:ss') + ' ' + zone
    }
    updateClock()
    window.setInterval(updateClock, 30000)
}())
