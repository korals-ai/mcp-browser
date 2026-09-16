// The pairing token: minted once per browser profile, kept in this
// extension origin's localStorage. A client that presents it in the connect
// URL is connected without the approval dialog, so it is a credential — the
// page shows it once, as the server-side variable to set, and can mint a
// new one.
//
// Derived from packages/extension/src/ui/authToken.tsx in microsoft/playwright
// (Apache-2.0, Copyright (c) Microsoft Corporation); rewritten without React.

import { TOKEN_ENV_NAME } from '../protocol';

const STORAGE_KEY = 'auth-token';

export function getOrCreateAuthToken(): string {
  let token = localStorage.getItem(STORAGE_KEY);
  if (!token) {
    token = generateAuthToken();
    localStorage.setItem(STORAGE_KEY, token);
  }
  return token;
}

export function regenerateAuthToken(): string {
  const token = generateAuthToken();
  localStorage.setItem(STORAGE_KEY, token);
  return token;
}

export function tokenEnvLine(token: string): string {
  return `${TOKEN_ENV_NAME}=${token}`;
}

function generateAuthToken(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

// The token box, with copy and regenerate. Shared by the connect and status pages.
export function renderAuthTokenSection(container: HTMLElement): void {
  container.innerHTML = `
    <div class="token-section">
      <div class="token-description">Set this on the server to skip this dialog next time:</div>
      <div class="token-row">
        <code class="token-code"></code>
        <button class="icon-button token-regenerate" title="Generate a new token" aria-label="Generate a new token">↻</button>
        <button class="icon-button token-copy" title="Copy" aria-label="Copy">⧉</button>
      </div>
    </div>`;
  const code = container.querySelector<HTMLElement>('.token-code')!;
  const copy = container.querySelector<HTMLButtonElement>('.token-copy')!;
  const regenerate = container.querySelector<HTMLButtonElement>('.token-regenerate')!;
  const show = (token: string) => { code.textContent = tokenEnvLine(token); };
  show(getOrCreateAuthToken());
  regenerate.addEventListener('click', () => show(regenerateAuthToken()));
  copy.addEventListener('click', async () => {
    await navigator.clipboard.writeText(code.textContent ?? '');
    copy.textContent = '✓';
    setTimeout(() => { copy.textContent = '⧉'; }, 1500);
  });
}
