// Run with node tests/test_web.js. Exercises the actual browser configuration code.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = Object.fromEntries(['address_list', 'pv_name', 'search_mode', 'auto_address'].map(name => [name, {value: '', checked: false}]));
const nodes = {'#config-form': {elements}, '#config-source': {}, '#address-options': {}, '#manual-broadcast': {}};
const context = vm.createContext({document: {querySelector: key => nodes[key] || {content: 'test'}}, console});
vm.runInContext(fs.readFileSync('epics_field_client/web/app.js', 'utf8').split('const toolLabels =')[0], context);
function evaluate(code) { return vm.runInContext(code, context); }
evaluate('defaults = {server_port:5064, repeater_port:5065, max_array_bytes:16384};');
elements.pv_name.value = 'TEST:TEMP';
elements.search_mode.value = 'inherit';
assert.equal(evaluate('configuration("real").connection_mode'), 'inherit', 'No environment variables must still allow CA defaults');
assert.equal(evaluate('configuration("real").address_list'), '');
evaluate('sourceHint()');
assert.match(nodes['#config-source'].textContent, /默认|自动/);
elements.search_mode.value = 'broadcast';
elements.address_list.value = 'stale-gateway';
assert.equal(evaluate('configuration("real").auto_address'), true);
assert.equal(evaluate('configuration("real").address_list'), '', 'Broadcast must not use a stale manual address');
elements.search_mode.value = 'manual';
assert.equal(evaluate('configuration("real").address_list'), 'stale-gateway');
assert.equal(evaluate('configuration("real").auto_address'), false);
elements.auto_address.checked = true;
assert.equal(evaluate('configuration("real").auto_address'), true);
elements.search_mode.value = 'inherit';
evaluate('detectedCA = {EPICS_CA_ADDR_LIST:"ioc.local", EPICS_CA_AUTO_ADDR_LIST:"NO", EPICS_CA_SERVER_PORT:"6000"};');
assert.equal(evaluate('configuration("real").connection_mode'), 'inherit');
assert.equal(evaluate('configuration("real").server_port'), 6000);
evaluate('sourceHint()');
assert.match(nodes['#config-source'].textContent, /ioc.local/);
evaluate('hasNameServer = true; sourceHint()');
assert.match(nodes['#config-source'].textContent, /不支持/);
context.bootstrap = {defaults: {server_port:5064, repeater_port:5065, max_array_bytes:16384, timeout_s:3},
  detected_ca_environment: {EPICS_CA_ADDR_LIST:'environment-gateway'}, name_server_configured:false,
  saved_config: {connection_mode:'explicit', address_list:'saved-gateway', auto_address:true, pv_name:'SAVED:TEMP', server_port:6000, repeater_port:6001, max_array_bytes:1024, timeout_s:8}, has_saved_config:true};
evaluate('restoreConfiguration(bootstrap)');
assert.equal(elements.search_mode.value, 'manual');
assert.equal(evaluate('configuration("real").address_list'), 'saved-gateway');
assert.equal(evaluate('configuration("real").auto_address'), true);
for (const key of ['server_port', 'repeater_port', 'max_array_bytes', 'timeout_s']) {
  assert.equal(evaluate(`configuration("real").${key}`), context.bootstrap.saved_config[key]);
}
context.bootstrap.saved_config.address_list = '';
evaluate('restoreConfiguration(bootstrap)');
assert.equal(elements.search_mode.value, 'broadcast');
context.bootstrap.saved_config.connection_mode = 'inherit';
evaluate('restoreConfiguration(bootstrap)');
assert.equal(elements.search_mode.value, 'inherit');
context.bootstrap.has_saved_config = false;
evaluate('restoreConfiguration(bootstrap)');
assert.equal(elements.search_mode.value, 'inherit');
assert.equal(evaluate('configuration("real").server_port'), 5064);
evaluate('detectedCA.EPICS_CA_ADDR_LIST = "ioc1,ioc2"');
assert.throws(() => evaluate('configuration("real")'), /手动模式/);
console.log('Browser configuration checks passed');
