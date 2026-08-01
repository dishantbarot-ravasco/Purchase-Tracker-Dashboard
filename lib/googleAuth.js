/**
 * Shared helper: turns the service account key (from the SERVICE_ACCOUNT_KEY
 * env var) into a short-lived access token for a given scope, using
 * google-auth-library's JWT client directly. Kept in its own file since both
 * drive.js (Drive scope) and roles.js (Sheets scope) need this.
 */
'use strict';

const { JWT } = require('google-auth-library');

// Cache one JWT client per scope so we're not re-parsing the service
// account key on every request - google-auth-library already caches and
// refreshes the underlying access token internally as it nears expiry.
const clientCache = {};

function getClient(scope) {
  if (clientCache[scope]) return clientCache[scope];
  const credentials = JSON.parse(process.env.SERVICE_ACCOUNT_KEY);
  const client = new JWT({
    email: credentials.client_email,
    key: credentials.private_key,
    scopes: [scope]
  });
  clientCache[scope] = client;
  return client;
}

async function getAccessToken(scope) {
  const client = getClient(scope);
  const { token } = await client.getAccessToken();
  return token;
}

module.exports = { getAccessToken };
