document.addEventListener('click', event => {
  const transitionLink = event.target.closest('[data-auth-transition]');
  if (transitionLink && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) {
    event.preventDefault();
    const shell = document.querySelector('.auth-shell');
    if (!shell) {
      window.location.assign(transitionLink.href);
      return;
    }
    shell.classList.add('auth-shell-leaving');
    window.setTimeout(() => window.location.assign(transitionLink.href), 420);
    return;
  }

  const button = event.target.closest('.password-toggle');
  if (!button) return;
  const field = document.getElementById(button.dataset.target);
  if (!field) return;
  const reveal = field.type === 'password';
  field.type = reveal ? 'text' : 'password';
  button.innerHTML = `<i class="bi bi-eye${reveal ? '-slash' : ''}"></i>`;
  button.setAttribute('aria-label', reveal ? 'Ocultar contraseña' : 'Mostrar contraseña');
  button.title = reveal ? 'Ocultar contraseña' : 'Mostrar contraseña';
});

const sidebar = document.getElementById('app-sidebar');
const collapseButton = document.querySelector('.sidebar-collapse');
const mobileMenuButton = document.querySelector('.mobile-menu-toggle');

if (sidebar && collapseButton) {
  const applyCollapsedState = collapsed => {
    document.body.classList.toggle('sidebar-collapsed', collapsed);
    collapseButton.setAttribute('aria-expanded', String(!collapsed));
    collapseButton.setAttribute('aria-label', collapsed ? 'Expandir menú' : 'Contraer menú');
    collapseButton.title = collapsed ? 'Expandir menú' : 'Contraer menú';
  };

  applyCollapsedState(localStorage.getItem('smartfact-sidebar-collapsed') === 'true');
  collapseButton.addEventListener('click', () => {
    const collapsed = !document.body.classList.contains('sidebar-collapsed');
    localStorage.setItem('smartfact-sidebar-collapsed', String(collapsed));
    applyCollapsedState(collapsed);
  });
}

if (sidebar && mobileMenuButton) {
  mobileMenuButton.addEventListener('click', () => {
    const open = document.body.classList.toggle('sidebar-open');
    mobileMenuButton.setAttribute('aria-expanded', String(open));
    mobileMenuButton.setAttribute('aria-label', open ? 'Cerrar menú' : 'Abrir menú');
  });

  document.addEventListener('click', event => {
    if (document.body.classList.contains('sidebar-open')
      && !sidebar.contains(event.target)
      && !mobileMenuButton.contains(event.target)) {
      document.body.classList.remove('sidebar-open');
      mobileMenuButton.setAttribute('aria-expanded', 'false');
      mobileMenuButton.setAttribute('aria-label', 'Abrir menú');
    }
  });
}

const registerPassword = document.getElementById('password');
const confirmPassword = document.getElementById('confirm-password');

if (registerPassword && document.getElementById('password-rules')) {
  const rules = {
    length: value => value.length >= 10,
    uppercase: value => /[A-Z]/.test(value),
    lowercase: value => /[a-z]/.test(value),
    number: value => /\d/.test(value),
    special: value => /[^A-Za-z0-9]/.test(value)
  };

  const updatePasswordFeedback = () => {
    const value = registerPassword.value;
    let allRulesMet = true;

    Object.entries(rules).forEach(([name, check]) => {
      const item = document.querySelector(`[data-rule="${name}"]`);
      const met = check(value);
      allRulesMet = allRulesMet && met;
      if (!item) return;
      item.classList.toggle('is-met', met);
      item.querySelector('i').className = `bi ${met ? 'bi-check-circle-fill' : 'bi-circle'}`;
    });

    registerPassword.setCustomValidity(value && !allRulesMet
      ? 'La contraseña todavía no cumple todos los requisitos.'
      : '');

    if (confirmPassword) {
      const mismatch = confirmPassword.value && confirmPassword.value !== value;
      confirmPassword.setCustomValidity(mismatch ? 'Las contraseñas no coinciden.' : '');
      const status = document.getElementById('confirm-password-status');
      if (status) {
        status.textContent = mismatch
          ? 'Las contraseñas no coinciden.'
          : confirmPassword.value ? 'Las contraseñas coinciden.' : 'Vuelve a escribir la contraseña para verificarla.';
        status.classList.toggle('is-invalid', Boolean(mismatch));
        status.classList.toggle('is-valid', Boolean(confirmPassword.value) && !mismatch);
      }
    }
  };

  registerPassword.addEventListener('input', updatePasswordFeedback);
  confirmPassword?.addEventListener('input', updatePasswordFeedback);
  updatePasswordFeedback();
}
