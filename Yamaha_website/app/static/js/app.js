/* Small helpers for the Bike Factory Machine Issue Portal. No libraries needed. */
(function () {
  'use strict';

  var $ = function (selector, root) { return (root || document).querySelector(selector); };
  var $$ = function (selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); };

  /* ---- mobile menu ---- */
  var menuButton = $('#menu-btn');
  var scrim = $('#scrim');
  function setMenu(open) { document.body.classList.toggle('nav-open', open); }
  if (menuButton) {
    menuButton.addEventListener('click', function () { setMenu(!document.body.classList.contains('nav-open')); });
  }
  if (scrim) { scrim.addEventListener('click', function () { setMenu(false); }); }
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { setMenu(false); } });

  /* ---- ask before risky actions: <form data-confirm="Are you sure?"> ---- */
  document.addEventListener('submit', function (e) {
    var message = e.target.getAttribute && e.target.getAttribute('data-confirm');
    if (message && !window.confirm(message)) { e.preventDefault(); }
  });

  /* ---- flash messages: close button + auto hide ---- */
  function dismiss(flash) {
    flash.classList.add('flash-hide');
    setTimeout(function () { if (flash.parentNode) { flash.parentNode.removeChild(flash); } }, 250);
  }
  document.addEventListener('click', function (e) {
    if (e.target.classList && e.target.classList.contains('flash-close')) { dismiss(e.target.closest('.flash')); }
  });
  $$('.flash-success, .flash-info').forEach(function (flash) { setTimeout(function () { dismiss(flash); }, 7000); });

  /* ---- show / hide password ---- */
  $$('[data-toggle-password]').forEach(function (button) {
    button.addEventListener('click', function () {
      var input = button.parentElement.querySelector('input');
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      button.textContent = show ? 'Hide' : 'Show';
    });
  });

  /* ---- report form: department follows the machine ---- */
  var machine = $('#machine_id');
  var department = $('#department');
  if (machine && department) {
    var syncDepartment = function () {
      var option = machine.options[machine.selectedIndex];
      department.value = option ? (option.getAttribute('data-department') || '') : '';
    };
    machine.addEventListener('change', syncDepartment);
    syncDepartment();
  }

  /* ---- character counter ---- */
  $$('textarea[data-counter]').forEach(function (box) {
    var target = $('[data-counter-for="' + box.id + '"]');
    if (!target) { return; }
    var update = function () { target.textContent = box.value.length; };
    box.addEventListener('input', update);
    update();
  });

  /* ---- chosen photo name ---- */
  var photo = $('#photo');
  var photoName = $('#photo-name');
  if (photo && photoName) {
    var original = photoName.textContent;
    photo.addEventListener('change', function () {
      photoName.textContent = photo.files && photo.files.length ? photo.files[0].name : original;
    });
  }

  /* ---- manager: near real-time alerts (checks the server every 15 seconds) ---- */
  var pollUrl = document.body.getAttribute('data-poll');
  if (pollUrl) {
    var lastCount = null;
    var toasts = $('#toasts');

    var showToast = function (item) {
      if (!toasts) { return; }
      var link = document.createElement('a');
      link.className = 'toast';
      link.href = item.url;
      var title = document.createElement('strong');
      title.textContent = 'New report: ' + item.title;
      var detail = document.createElement('span');
      var label = item.match === 'duplicate' ? 'Duplicate' : item.match === 'possible' ? 'Possible duplicate' : 'New issue';
      detail.textContent = item.machine + ' · ' + item.worker + ' · ' + label;
      link.appendChild(title);
      link.appendChild(detail);
      toasts.appendChild(link);
      setTimeout(function () { if (link.parentNode) { link.parentNode.removeChild(link); } }, 9000);
    };

    var setBadges = function (count) {
      $$('[data-unseen]').forEach(function (badge) {
        badge.textContent = count;
        badge.classList.toggle('hidden', count === 0);
      });
    };

    var poll = function () {
      fetch(pollUrl, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
        .then(function (response) {
          var type = response.headers.get('content-type') || '';
          if (!response.ok || type.indexOf('json') === -1) { throw new Error('not json'); }
          return response.json();
        })
        .then(function (data) {
          setBadges(data.count);
          if (lastCount !== null && data.count > lastCount) {
            data.latest.slice(0, data.count - lastCount).forEach(showToast);
          }
          lastCount = data.count;
        })
        .catch(function () { /* ignore: try again next time */ });
    };
    poll();
    setInterval(poll, 15000);
  }
})();
