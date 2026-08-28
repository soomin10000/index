// Session-expiry guard. When any fetch on the page comes back 401 (the login
// cookie expired or the server was restarted with a new secret), bounce to the
// login screen instead of letting every panel quietly render as an error.
(function () {
  var _fetch = window.fetch;
  window.fetch = function () {
    return _fetch.apply(this, arguments).then(function (r) {
      if (r.status === 401) location.href = '/login';
      return r;
    });
  };
})();
