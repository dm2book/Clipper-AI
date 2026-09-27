/**
 * URLs of RPC providers and Discord webhooks carry credentials (an `api-key`
 * query parameter, the webhook token in the path). Anything that may end up in
 * a log line goes through here first.
 */

const SECRET_PARAM = /key|token|secret|auth|signature|password/i;

export function redactUrl(raw: string): string {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return '[invalid-url]';
  }
  for (const name of [...url.searchParams.keys()]) {
    if (SECRET_PARAM.test(name)) url.searchParams.set(name, '***');
  }
  // https://discord.com/api/webhooks/{id}/{token}
  url.pathname = url.pathname.replace(/(\/webhooks\/[^/]+\/)[^/?]+/, '$1***');
  if (url.username || url.password) {
    url.username = '***';
    url.password = '';
  }
  return url.toString();
}
