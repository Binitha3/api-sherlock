(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ------------------------------------------------------------------
     Background video — make sure autoplay starts (muted + playsinline)
     ------------------------------------------------------------------ */
  var video = document.querySelector(".bg-video");
  if (video) {
    var playPromise = video.play();
    if (playPromise && typeof playPromise.catch === "function") {
      playPromise.catch(function () {
        /* Autoplay blocked — poster/black background stays visible */
      });
    }
  }

  /* ------------------------------------------------------------------
     Stats count-up
     easeOutCubic, duration 1500 + i*80ms, start offset 480 + i*90ms,
     triggered once via IntersectionObserver (threshold 0.25)
     ------------------------------------------------------------------ */
  function formatValue(value, decimals) {
    return value.toFixed(decimals);
  }

  function countUp(el, index) {
    var target = parseFloat(el.getAttribute("data-target"));
    var decimals = parseInt(el.getAttribute("data-decimals") || "0", 10);

    if (isNaN(target)) return;

    if (reduceMotion) {
      el.textContent = formatValue(target, decimals);
      return;
    }

    var duration = 1500 + index * 80;
    var offset = 480 + index * 90;
    var startTime = null;

    function frame(now) {
      if (startTime === null) startTime = now;
      var progress = Math.min((now - startTime) / duration, 1);
      var eased = 1 - Math.pow(1 - progress, 3);
      el.textContent = formatValue(target * eased, decimals);
      if (progress < 1) {
        requestAnimationFrame(frame);
      } else {
        el.textContent = formatValue(target, decimals);
      }
    }

    window.setTimeout(function () {
      requestAnimationFrame(frame);
    }, offset);
  }

  var stats = Array.prototype.slice.call(document.querySelectorAll(".stat"));
  var counters = Array.prototype.slice.call(document.querySelectorAll("[data-count]"));

  counters.forEach(function (el) {
    var stat = el.closest(".stat");
    el.setAttribute("data-index", String(stats.indexOf(stat)));
  });

  if (reduceMotion || !("IntersectionObserver" in window)) {
    counters.forEach(function (el) {
      countUp(el, parseInt(el.getAttribute("data-index"), 10) || 0);
    });
  } else {
    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          var el = entry.target;
          observer.unobserve(el);
          countUp(el, parseInt(el.getAttribute("data-index"), 10) || 0);
        });
      },
      { threshold: 0.25 }
    );
    counters.forEach(function (el) {
      observer.observe(el);
    });
  }

  /* ------------------------------------------------------------------
     Mobile menu
     ------------------------------------------------------------------ */
  var burger = document.querySelector(".burger");
  var overlay = document.getElementById("menu-overlay");
  var menu = document.getElementById("mobile-menu");

  if (burger && overlay && menu) {
    function isOpen() {
      return burger.getAttribute("aria-expanded") === "true";
    }

    function openMenu() {
      burger.setAttribute("aria-expanded", "true");
      burger.setAttribute("aria-label", "Close menu");
      burger.classList.add("is-open");
      overlay.hidden = false;
      menu.hidden = false;
      document.body.classList.add("menu-open");
    }

    function closeMenu() {
      burger.setAttribute("aria-expanded", "false");
      burger.setAttribute("aria-label", "Open menu");
      burger.classList.remove("is-open");
      overlay.hidden = true;
      menu.hidden = true;
      document.body.classList.remove("menu-open");
    }

    burger.addEventListener("click", function () {
      if (isOpen()) {
        closeMenu();
      } else {
        openMenu();
      }
    });

    overlay.addEventListener("click", closeMenu);

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && isOpen()) closeMenu();
    });

    menu.addEventListener("click", function (event) {
      if (event.target.closest("a")) closeMenu();
    });

    window.addEventListener("resize", function () {
      if (window.innerWidth > 720 && isOpen()) closeMenu();
    });
  }
})();