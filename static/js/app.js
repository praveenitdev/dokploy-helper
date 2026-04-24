const shell = document.querySelector('.app-shell');
const sidebarToggle = document.getElementById('sidebarToggle');
const infraMenuToggle = document.getElementById('infraMenuToggle');
const infraSubmenu = document.getElementById('infraSubmenu');
const submenuChevron = document.querySelector('.submenu-chevron');

function setButtonLoadingState(button, loadingText) {
  if (!button) {
    return;
  }

  if (!button.dataset.originalHtml) {
    button.dataset.originalHtml = button.innerHTML;
  }

  button.disabled = true;
  button.classList.add('is-loading');
  button.innerHTML = `<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>${loadingText}`;
}

if (sidebarToggle && shell) {
  sidebarToggle.addEventListener('click', () => {
    if (window.innerWidth <= 992) {
      shell.classList.toggle('sidebar-expanded');
      return;
    }

    shell.classList.toggle('sidebar-collapsed');
  });
}

if (infraMenuToggle && infraSubmenu) {
  infraMenuToggle.addEventListener('click', () => {
    infraSubmenu.classList.toggle('show');
    if (submenuChevron) {
      submenuChevron.style.transform = infraSubmenu.classList.contains('show')
        ? 'rotate(0deg)'
        : 'rotate(-90deg)';
    }
  });
}

document.querySelectorAll('.js-refresh-btn').forEach((button) => {
  button.addEventListener('click', () => {
    const loadingText = button.dataset.loadingText || 'Refreshing';
    setButtonLoadingState(button, loadingText);
    window.location.reload();
  });
});

document.querySelectorAll('form[data-loading="true"]').forEach((form) => {
  form.addEventListener('submit', () => {
    const submitButton = form.querySelector('[type="submit"]');
    const loadingText = submitButton?.dataset.loadingText || 'Processing';
    setButtonLoadingState(submitButton, loadingText);
  });
});
