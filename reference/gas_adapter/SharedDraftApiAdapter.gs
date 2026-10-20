/** Standalone future adapter; not installed in or modifying the existing GAS project. */
function createSharedDraftThroughApi_(request, apiBaseUrl, tokenProvider, identityTokenProvider) {
  if (!request || !request.operationKey || !request.clinicUuid || !request.clinicId ||
      !request.campaignId || !request.recipient || !request.subject || !request.body) {
    throw new Error('Shared Draft API request is incomplete');
  }
  if (typeof tokenProvider !== 'function') throw new Error('API token provider is required');
  var response;
  try {
    response = UrlFetchApp.fetch(apiBaseUrl.replace(/\/$/, '') + '/v1/drafts', {
      method: 'post', contentType: 'application/json', muteHttpExceptions: true,
      headers: identityTokenProvider
        ? {Authorization: 'Bearer ' + identityTokenProvider(), 'X-Shared-Draft-Token': tokenProvider()}
        : {Authorization: 'Bearer ' + tokenProvider()},
      payload: JSON.stringify({
        operationKey: String(request.operationKey), clinicUuid: String(request.clinicUuid),
        clinicId: Number(request.clinicId), campaignId: String(request.campaignId),
        gasCaseId: String(request.gasCaseId || ''), recipient: String(request.recipient),
        subject: String(request.subject), body: String(request.body),
        clinicName: String(request.clinicName || ''), clinicAddress: String(request.clinicAddress || '')
      })
    });
  } catch (error) {
    // A fetch timeout is UNKNOWN. Retry, if chosen by an operator, with the same operationKey.
    return {status: 'UNKNOWN', operationKey: String(request.operationKey), reason: 'HTTP outcome uncertain'};
  }
  var code = response.getResponseCode();
  var result;
  try { result = JSON.parse(response.getContentText()); }
  catch (error) { return {status: 'UNKNOWN', operationKey: String(request.operationKey)}; }
  if (code >= 500) result.status = 'UNKNOWN';
  return result;
}

/** Obtain a Cloud Run audience ID token using the GAS execution service account. */
function createCloudRunIdentityTokenProvider_(serviceAccountEmail, audience) {
  return function() {
    var url = 'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/' +
      encodeURIComponent(serviceAccountEmail) + ':generateIdToken';
    var response = UrlFetchApp.fetch(url, {
      method: 'post', contentType: 'application/json', muteHttpExceptions: true,
      headers: {Authorization: 'Bearer ' + ScriptApp.getOAuthToken()},
      payload: JSON.stringify({audience: audience, includeEmail: true})
    });
    if (response.getResponseCode() < 200 || response.getResponseCode() >= 300) {
      throw new Error('Cloud Run identity token unavailable');
    }
    var token = JSON.parse(response.getContentText()).token;
    if (!token) throw new Error('Cloud Run identity token unavailable');
    return token;
  };
}

/** Fetch the separate API credential from Secret Manager; never persist it in Script Properties. */
function createSecretManagerTokenProvider_(projectId, secretId, version) {
  return function() {
    var name = 'projects/' + encodeURIComponent(projectId) + '/secrets/' +
      encodeURIComponent(secretId) + '/versions/' + encodeURIComponent(version || 'latest');
    var response = UrlFetchApp.fetch('https://secretmanager.googleapis.com/v1/' + name + ':access', {
      method: 'get', muteHttpExceptions: true,
      headers: {Authorization: 'Bearer ' + ScriptApp.getOAuthToken()}
    });
    if (response.getResponseCode() < 200 || response.getResponseCode() >= 300) {
      throw new Error('Shared Draft API credential unavailable');
    }
    var encoded = JSON.parse(response.getContentText()).payload.data;
    if (!encoded) throw new Error('Shared Draft API credential unavailable');
    return Utilities.newBlob(Utilities.base64DecodeWebSafe(encoded)).getDataAsString('UTF-8');
  };
}

