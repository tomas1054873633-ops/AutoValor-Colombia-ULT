/* Se carga en <head>: aplica el tema guardado antes de pintar (sin parpadeos) y marca que hay JavaScript. */
(function () {
  var root = document.documentElement;
  root.classList.add('js');
  try {
    var saved = window.localStorage.getItem('av-theme');
    if (saved === 'light' || saved === 'dark') root.setAttribute('data-theme', saved);
  } catch (e) { /* sin almacenamiento disponible: se usa la preferencia del sistema */ }
})();
