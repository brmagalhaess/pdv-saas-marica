/* Applies the saved theme before the first paint (loaded without defer on purpose). */
(function () {
  var theme = 'light';
  try { theme = localStorage.getItem('pdv2.theme') || 'light'; } catch (error) { /* private mode */ }
  if (['light', 'dark', 'contrast'].indexOf(theme) < 0) theme = 'light';
  document.documentElement.dataset.theme = theme;
})();
