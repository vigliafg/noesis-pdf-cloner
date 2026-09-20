/* Noesis PDF Cloner — help site scripts */
(function () {
  'use strict';

  // Highlight the sidebar entry of the section currently in view.
  function updateActiveLink() {
    var links = document.querySelectorAll('nav.sidebar a');
    if (!links.length) return;
    var pos = window.scrollY + 90; // offset for the sticky topbar
    var current = links[0];
    for (var i = 0; i < links.length; i++) {
      var target = document.querySelector(links[i].getAttribute('href'));
      if (!target) continue;
      if (target.offsetTop <= pos) current = links[i];
    }
    for (var j = 0; j < links.length; j++) {
      links[j].classList.toggle('active', links[j] === current);
    }
  }

  var ticking = false;
  window.addEventListener('scroll', function () {
    if (!ticking) {
      window.requestAnimationFrame(function () {
        updateActiveLink();
        ticking = false;
      });
      ticking = true;
    }
  });
  window.addEventListener('resize', updateActiveLink);
  document.addEventListener('DOMContentLoaded', updateActiveLink);

  // Auto-link: every heading with an id gets an invisible anchor target.
  document.addEventListener('DOMContentLoaded', function () {
    var headings = document.querySelectorAll('main.content h2[id], main.content h3[id]');
    for (var i = 0; i < headings.length; i++) {
      headings[i].style.scrollMarginTop = '72px';
    }
  });
})();
