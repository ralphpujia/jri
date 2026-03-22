/* ==========================================================================
   Landing Page JS — Bouncing Logo
   ========================================================================== */
(function () {
    var SPEED = 1.2;   // px per frame
    var IMG_SIZE = 720; // display width of the logo

    var container = document.getElementById('bouncing-logo-container');
    var img = document.getElementById('bouncing-logo');
    if (!container || !img) return;

    var x = 0;
    var y = 0;
    var dx = SPEED;
    var dy = SPEED;
    var initialized = false;
    var raf = 0;

    function tick() {
        var imgHeight = img.naturalHeight
            ? (IMG_SIZE / img.naturalWidth) * img.naturalHeight
            : IMG_SIZE;
        var maxX = window.innerWidth - IMG_SIZE;
        var maxY = window.innerHeight - imgHeight;

        if (!initialized) {
            x = Math.random() * Math.max(0, maxX);
            y = Math.random() * Math.max(0, maxY);
            dx = (Math.random() > 0.5 ? 1 : -1) * SPEED;
            dy = (Math.random() > 0.5 ? 1 : -1) * SPEED;
            initialized = true;
        }

        x += dx;
        y += dy;

        if (x <= 0 || x >= maxX) {
            dx *= -1;
            x = Math.max(0, Math.min(x, maxX));
        }
        if (y <= 0 || y >= maxY) {
            dy *= -1;
            y = Math.max(0, Math.min(y, maxY));
        }

        img.style.transform = 'translate(' + x + 'px, ' + y + 'px)';
        raf = requestAnimationFrame(tick);
    }

    function start() {
        raf = requestAnimationFrame(tick);
    }

    if (img.complete) {
        start();
    } else {
        img.addEventListener('load', start);
    }
})();
