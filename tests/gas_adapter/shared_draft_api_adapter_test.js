const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('reference/gas_adapter/SharedDraftApiAdapter.gs', 'utf8');
let calls = [];
let failure = false;
const context = {
  UrlFetchApp: {fetch(url, options) {
    calls.push({url, options});
    if (failure) throw new Error('synthetic timeout');
    if (url.includes(':generateIdToken')) return {getResponseCode: () => 200,
      getContentText: () => JSON.stringify({token: 'minted-id-token'})};
    if (url.includes(':access')) return {getResponseCode: () => 200,
      getContentText: () => JSON.stringify({payload: {data: Buffer.from('secret-manager-api-token').toString('base64url')}})};
    return {getResponseCode: () => 200,
      getContentText: () => JSON.stringify({status: 'DRAFT_CREATED', draftId: 'mock-draft'})};
  }},
  ScriptApp: {getOAuthToken: () => 'mock-gas-oauth'},
  Utilities: {
    base64DecodeWebSafe: (value) => Buffer.from(value, 'base64url'),
    newBlob: (value) => ({getDataAsString: () => Buffer.from(value).toString('utf8')}),
  },
  JSON, String, Number, Error, Buffer,
};
vm.createContext(context);
vm.runInContext(source, context);

const request = {operationKey: 'op-20261020-101', clinicUuid: '123e4567-e89b-42d3-a456-426614174000',
  clinicId: 101, campaignId: 'campaign-a', gasCaseId: 'gas-case-1',
  recipient: 'clinic@example.jp', subject: 'Subject', body: 'Body'};
const result = context.createSharedDraftThroughApi_(request, 'https://api.example.invalid/', () => 'mock-token');
assert.equal(result.status, 'DRAFT_CREATED');
assert.equal(calls.length, 1);
assert.equal(calls[0].url, 'https://api.example.invalid/v1/drafts');
assert.equal(calls[0].options.headers.Authorization, 'Bearer mock-token');
assert.equal(JSON.parse(calls[0].options.payload).operationKey, request.operationKey);

context.createSharedDraftThroughApi_(request, 'https://api.example.invalid', () => 'api-secret', () => 'iam-id-token');
assert.equal(calls[1].options.headers.Authorization, 'Bearer iam-id-token');
assert.equal(calls[1].options.headers['X-Shared-Draft-Token'], 'api-secret');
const minted = context.createCloudRunIdentityTokenProvider_('api-invoker@example.iam.gserviceaccount.com', 'https://api.example.invalid')();
assert.equal(minted, 'minted-id-token');
const apiSecret = context.createSecretManagerTokenProvider_('nonprod-project', 'shared-draft-api-token', 'latest')();
assert.equal(apiSecret, 'secret-manager-api-token');

failure = true;
const unknown = context.createSharedDraftThroughApi_(request, 'https://api.example.invalid', () => 'mock-token');
assert.equal(unknown.status, 'UNKNOWN');
assert.equal(unknown.operationKey, request.operationKey);
assert.equal(calls.length, 5); // One call per explicit invocation/provider; no internal retry.
console.log('GAS adapter mock integration: PASS (5 fixture calls, no external request)');
