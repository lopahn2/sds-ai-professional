import {test} from '@playwright/test';

// Isolated API fixtures mimic the non-secret challenge a gateway supplies with the document.
export const mockNonce = 'a'.repeat(64) + '.' + 'b'.repeat(64);
export function mockLocalOperator(page) {
  const base = new URL(test.info().project.use.baseURL);
  if (base.protocol !== 'http:' || !base.port || !['127.0.0.1', 'localhost'].includes(base.hostname)) {
    throw new Error('Mock operator requires a dedicated loopback test origin');
  }
  return page.addInitScript(({origin, nonce}) => {
    if(location.origin === origin) document.cookie = 'lp_operator_csrf=' + nonce + '; Path=/; SameSite=Strict';
  }, {origin:base.origin, nonce:mockNonce});
}
