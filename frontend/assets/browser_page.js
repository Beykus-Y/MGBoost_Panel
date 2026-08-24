(() => {
  const button = document.getElementById('copy-btn');
  const display = document.getElementById('url-display');
  if (!button || !display) return;

  const done = () => {
    button.textContent = '✓ Скопировано';
    button.classList.add('ok');
    setTimeout(() => {
      button.textContent = 'Копировать';
      button.classList.remove('ok');
    }, 2000);
  };

  const fallback = () => {
    const textarea = document.createElement('textarea');
    textarea.value = display.textContent || '';
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    try {
      document.execCommand('copy');
    } catch {
      // The browser did not permit the legacy clipboard fallback.
    }
    textarea.remove();
    done();
  };

  button.addEventListener('click', () => {
    const url = display.textContent || '';
    if (navigator.clipboard) {
      navigator.clipboard.writeText(url).then(done).catch(fallback);
    } else {
      fallback();
    }
  });
})();
